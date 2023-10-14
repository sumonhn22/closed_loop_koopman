"""Define and automate tasks with ``doit``."""

import pathlib
import shutil
import subprocess

import control
import doit
import joblib
import numpy as np
import optuna
import pykoop
import tomli
from matplotlib import pyplot as plt

import cl_koopman_pipeline

# Have ``pykoop`` skip validation for performance improvements
pykoop.set_config(skip_validation=True)

# Directory containing ``dodo.py``
WD = pathlib.Path(__file__).parent.resolve()

# Random seeds
OPTUNA_TPE_SEED = 3501
SKLEARN_SPLIT_SEED = 1234


def task_preprocess_experiments():
    """Pickle raw CSV files."""
    datasets = [
        ('controller_20230828', 'training_controller'),
        ('controller_20230915', 'test_controller'),
    ]
    for (path, name) in datasets:
        dataset = WD.joinpath('dataset').joinpath(path)
        experiment = WD.joinpath(
            'build',
            'experiments',
            f'dataset_{name}.pickle',
        )
        yield {
            'name': name,
            'actions': [(action_preprocess_experiments, (
                dataset,
                experiment,
            ))],
            'targets': [experiment],
            'uptodate': [doit.tools.check_timestamp_unchanged(str(dataset))],
            'clean': True,
        }


def task_plot_experiments():
    """Plot pickled data."""
    datasets = ['training_controller', 'test_controller']
    for name in datasets:
        experiment = WD.joinpath(
            'build',
            'experiments',
            f'dataset_{name}.pickle',
        )
        plot_dir = WD.joinpath('build', 'plots', name)
        yield {
            'name': name,
            'actions': [(action_plot_experiments, (experiment, plot_dir))],
            'file_dep': [experiment],
            'targets': [plot_dir],
            'clean': [(shutil.rmtree, [plot_dir, True])],
        }


def task_save_lifting_functions():
    """Save lifting functions for shared use."""
    lf_path = WD.joinpath(
        'build',
        'lifting_functions',
        'lifting_functions.pickle',
    )
    return {
        'actions': [(action_save_lifting_functions, (lf_path, ))],
        'targets': [lf_path],
        'clean': True,
    }


def task_run_cross_validation():
    """Run cross-validation."""
    experiment = WD.joinpath(
        'build',
        'experiments',
        'dataset_training_controller.pickle',
    )
    lifting_functions = WD.joinpath(
        'build',
        'lifting_functions',
        'lifting_functions.pickle',
    )
    for study_type in ['closed_loop', 'open_loop']:
        study = WD.joinpath('build', 'studies', f'{study_type}.db')
        yield {
            'name':
            study_type,
            'actions': [(action_run_cross_validation, (
                experiment,
                lifting_functions,
                study,
                study_type,
            ))],
            'file_dep': [experiment, lifting_functions],
            'targets': [study],
            'clean':
            True,
        }


def task_evaluate_models():
    """Evaluate cross-validation results."""
    lifting_functions = WD.joinpath(
        'build',
        'lifting_functions',
        'lifting_functions.pickle',
    )
    study_cl = WD.joinpath('build', 'studies', 'closed_loop.db')
    study_ol = WD.joinpath('build', 'studies', 'open_loop.db')
    experiment_training_controller = WD.joinpath(
        'build',
        'experiments',
        'dataset_training_controller.pickle',
    )
    experiment_test_controller = WD.joinpath(
        'build',
        'experiments',
        'dataset_test_controller.pickle',
    )
    results = WD.joinpath('build', 'results', 'results.pickle')
    return {
        'actions': [(action_evaluate_models, (
            lifting_functions,
            study_cl,
            study_ol,
            experiment_training_controller,
            experiment_test_controller,
            results,
        ))],
        'file_dep': [lifting_functions, study_cl, study_ol],
        'targets': [results],
        'clean':
        True,
    }


def action_preprocess_experiments(
    dataset_path: pathlib.Path,
    experiment_path: pathlib.Path,
):
    """Pickle raw CSV files."""
    # Sampling timestep
    t_step = 1 / 500
    # Number of validation episodes
    n_valid = 2
    # Number of points to skip at the beginning of each episode
    n_skip = 500
    # Parse controller config
    with open(dataset_path.joinpath('controller.toml'), 'rb') as f:
        gains = tomli.load(f)
    # Set up controller
    derivative = control.TransferFunction(
        [
            gains['tau'] / (1 + gains['tau'] * t_step),
            -gains['tau'] / (1 + gains['tau'] * t_step),
        ],
        [
            1,
            -1 / (1 + gains['tau'] * t_step),
        ],
        dt=True,
    )
    K_theta = control.tf2io(
        -gains['kp_theta'] - gains['kd_theta'] * derivative,
        name='K_theta',
    )
    K_alpha = control.tf2io(
        -gains['kp_alpha'] - gains['kd_alpha'] * derivative,
        name='K_alpha',
    )
    sum = control.summing_junction(inputs=2, name='sum')
    K = control.interconnect(
        [K_theta, K_alpha, sum],
        connections=[
            ['sum.u[0]', 'K_theta.y[0]'],
            ['sum.u[1]', 'K_alpha.y[0]'],
        ],
        inplist=['K_theta.u[0]', 'K_alpha.u[0]'],
        outlist=['sum.y'],
    )
    controller_ss = control.StateSpace(
        K.A,
        K.B,
        K.C,
        K.D,
        dt=True,
    )
    controller = (
        controller_ss.A,
        controller_ss.B,
        controller_ss.C,
        controller_ss.D,
    )
    C_plant = np.eye(2)
    # Parse CSVs
    episodes_ol = []
    episodes_cl = []
    for (ep, file) in enumerate(sorted(dataset_path.glob('*.csv'))):
        data = np.loadtxt(file, skiprows=1, delimiter=',')
        t = data[:, 0]
        target_theta = data[:, 1]
        target_alpha = data[:, 2]
        theta = data[:, 3]
        alpha = data[:, 4]
        t_step = t[1] - t[0]
        v = data[:, 5]  # noqa: F841
        ff = data[:, 6]
        vf = data[:, 7]  # ``v + f`` with saturation
        error = np.vstack([
            target_theta - theta,
            target_alpha - alpha,
        ])
        t, y, x = control.forced_response(
            sys=control.StateSpace(*controller, dt=True),
            T=t,
            U=error,
            return_x=True,
        )
        X_ep_ol = np.vstack([
            theta,
            alpha,
            vf,
        ]).T
        X_ep_cl = np.vstack([
            x,
            theta,
            alpha,
            target_theta,
            target_alpha,
            ff,
        ]).T
        episodes_ol.append((ep, X_ep_ol[n_skip:, :]))
        episodes_cl.append((ep, X_ep_cl[n_skip:, :]))
    # Combine episodes
    n_train = len(episodes_ol) - n_valid
    X_ol_train = pykoop.combine_episodes(
        episodes_ol[:n_train],
        episode_feature=True,
    )
    X_ol_valid = pykoop.combine_episodes(
        episodes_ol[n_train:],
        episode_feature=True,
    )
    X_cl_train = pykoop.combine_episodes(
        episodes_cl[:n_train],
        episode_feature=True,
    )
    X_cl_valid = pykoop.combine_episodes(
        episodes_cl[n_train:],
        episode_feature=True,
    )
    # Format output
    output_dict = {
        't_step': t_step,
        'open_loop': {
            'X_train': X_ol_train,
            'X_test': X_ol_valid,
            'episode_feature': True,
            'n_inputs': 1,
        },
        'closed_loop': {
            'X_train': X_cl_train,
            'X_test': X_cl_valid,
            'n_inputs': 3,
            'episode_feature': True,
            'controller': controller,
            'C_plant': C_plant,
        },
    }
    # Save pickle
    experiment_path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(output_dict, experiment_path)


def action_plot_experiments(
    experiment_path: pathlib.Path,
    plot_path: pathlib.Path,
):
    """Plot pickled data."""
    # Load pickle
    dataset = joblib.load(experiment_path)
    # Split episodes
    eps_ol = pykoop.split_episodes(
        dataset['open_loop']['X_train'],
        episode_feature=dataset['open_loop']['episode_feature'],
    )
    eps_cl = pykoop.split_episodes(
        dataset['closed_loop']['X_train'],
        episode_feature=dataset['closed_loop']['episode_feature'],
    )
    # Create plots
    fig_d, ax_d = plt.subplots(
        8,
        1,
        constrained_layout=True,
        sharex=True,
    )
    fig_c, ax_c = plt.subplots(
        1,
        3,
        constrained_layout=True,
        sharex=True,
    )
    for (i, X_ol_i), (_, X_cl_i) in zip(eps_ol, eps_cl):
        # Controller states
        ax_d[0].plot(X_cl_i[:, 0])
        ax_d[0].set_ylabel(r'$x_0[k]$')
        ax_d[1].plot(X_cl_i[:, 1])
        ax_d[1].set_ylabel(r'$x_1[k]$')
        # Plant states
        ax_d[2].plot(X_cl_i[:, 2])
        ax_d[2].set_ylabel(r'$\theta[k]$')
        ax_d[3].plot(X_cl_i[:, 3])
        ax_d[3].set_ylabel(r'$\alpha[k]$')
        # System inputs
        ax_d[4].plot(X_cl_i[:, 4])
        ax_d[4].set_ylabel(r'$r_\theta[k]$')
        ax_d[5].plot(X_cl_i[:, 5])
        ax_d[5].set_ylabel(r'$r_\alpha[k]$')
        ax_d[6].plot(X_cl_i[:, 6])
        ax_d[6].set_ylabel(r'$f[k]$')
        # Saturated plant input
        ax_d[7].plot(X_ol_i[:, 2])
        ax_d[7].set_ylabel(r'$u[k]$')
        # Timestep
        ax_d[7].set_xlabel(r'$k$')
        # Simulate controller response
        t = np.arange(X_cl_i.shape[0]) * dataset['t_step']
        error = np.vstack([
            X_cl_i[:, 4] - X_cl_i[:, 2],
            X_cl_i[:, 5] - X_cl_i[:, 3],
        ])
        ff = X_cl_i[:, 6]
        controller = dataset['closed_loop']['controller']
        t, y, x = control.forced_response(
            sys=control.StateSpace(*controller, dt=True),
            T=t,
            U=error,
            X0=X_cl_i[0, :controller[0].shape[0]],
            return_x=True,
        )
        # Plot controller response
        ax_c[0].plot(ff + y[0, :])
        ax_c[0].set_ylabel(r'Simulated $u[k]$')
        ax_c[0].set_xlabel(r'$k$')
        ax_c[1].plot(X_ol_i[:, 2])
        ax_c[1].set_ylabel(r'Measured $u[k]$')
        ax_c[1].set_xlabel(r'$k$')
        ax_c[2].plot(ff + y[0, :] - X_ol_i[:, 2])
        ax_c[2].set_ylabel(r'$\Delta u[k]$')
        ax_c[2].set_xlabel(r'$k$')
    # Save plots
    plot_path.mkdir(parents=True, exist_ok=True)
    fig_d.savefig(plot_path.joinpath('dataset.png'))
    fig_c.savefig(plot_path.joinpath('controller_output.png'))
    plt.close(fig_d)
    plt.close(fig_c)


def action_save_lifting_functions(lifting_function_path: pathlib.Path):
    """Save lifting functions for shared use."""
    lifting_functions = [
        (
            'poly',
            pykoop.PolynomialLiftingFn(order=2),
        ),
        (
            'delay',
            pykoop.DelayLiftingFn(
                n_delays_state=10,
                n_delays_input=10,
            ),
        ),
    ]
    lifting_function_path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(lifting_functions, lifting_function_path)


def action_run_cross_validation(
    experiment_path: pathlib.Path,
    lifting_functions_path: pathlib.Path,
    study_path: pathlib.Path,
    study_type: str,
):
    """Run cross-validation."""
    # Delete database file if it exists
    study_path.unlink(missing_ok=True)
    # Create directory for database if it does not already exist
    study_path.parent.mkdir(parents=True, exist_ok=True)
    # Create study and run optimization
    storage_url = f'sqlite:///{study_path.resolve()}'
    optuna.create_study(
        storage=storage_url,
        sampler=optuna.samplers.TPESampler(seed=OPTUNA_TPE_SEED),
        pruner=optuna.pruners.ThresholdPruner(lower=-10),
        study_name=study_type,
        direction='maximize',
    )
    script_path = WD.joinpath(f'optuna_study_{study_type}.py')
    # Set number of processes
    n_processes = 6
    # Set number of trials per process
    n_trials = 30
    # Spawn processes and wait for them all to complete
    processes = []
    for i in range(n_processes):
        p = subprocess.Popen([
            'python',
            script_path.resolve(),
            experiment_path.resolve(),
            lifting_functions_path.resolve(),
            storage_url,
            str(n_trials),
            str(SKLEARN_SPLIT_SEED),
        ])
        processes.append(p)
    for p in processes:
        return_code = p.wait()
        if return_code < 0:
            raise RuntimeError(f'Process {p.pid} returned code {return_code}.')


def action_evaluate_models(
    lifting_functions_path: pathlib.Path,
    study_path_cl: pathlib.Path,
    study_path_ol: pathlib.Path,
    experiment_path_training_controller: pathlib.Path,
    experiment_path_test_controller: pathlib.Path,
    results_path: pathlib.Path,
):
    """Evaluate cross-validation results."""
    # Load studies
    study_cl = optuna.load_study(
        study_name='closed_loop',
        storage=f'sqlite:///{study_path_cl.resolve()}',
    )
    study_ol = optuna.load_study(
        study_name='open_loop',
        storage=f'sqlite:///{study_path_ol.resolve()}',
    )
    # Load datasets
    exp_train = joblib.load(experiment_path_training_controller)
    exp_test = joblib.load(experiment_path_test_controller)
    # Set shared lifting functions
    lifting_functions = joblib.load(lifting_functions_path)
    # Re-fit closed-loop model with all data
    kp_cl = cl_koopman_pipeline.ClKoopmanPipeline(
        lifting_functions=lifting_functions,
        regressor=cl_koopman_pipeline.ClEdmdConstrainedOpt(
            alpha=study_cl.best_params['alpha'],
            picos_eps=1e-6,
            solver_params={'solver': 'mosek'},
        ),
        controller=exp_train['closed_loop']['controller'],
        C_plant=exp_train['closed_loop']['C_plant'],
    )
    kp_cl.fit(
        exp_train['closed_loop']['X_train'],
        n_inputs=exp_train['closed_loop']['n_inputs'],
        episode_feature=exp_train['closed_loop']['episode_feature'],
    )
    # Re-fit open-loop model with all data
    kp_ol = pykoop.KoopmanPipeline(
        lifting_functions=[(
            'split',
            pykoop.SplitPipeline(
                lifting_functions_state=lifting_functions,
                lifting_functions_input=None,
            ),
        )],
        regressor=pykoop.Edmd(alpha=study_ol.best_params['alpha']),
    )
    # Fit model
    kp_ol.fit(
        exp_train['open_loop']['X_train'],
        n_inputs=exp_train['open_loop']['n_inputs'],
        episode_feature=exp_train['open_loop']['episode_feature'],
    )
    # Get test controller state space realization and output matrix
    Ac, Bc, Cc, Dc = exp_test['closed_loop']['controller']
    C_plant = exp_test['closed_loop']['C_plant']
    # Calculate new closed-loop Koopman matrix from the identified Koopman
    # plant model
    Up_cl = kp_cl.kp_plant_.regressor_.coef_.T
    Ap_cl = Up_cl[:, :Up_cl.shape[0]]
    Bp_cl = Up_cl[:, Up_cl.shape[0]:]
    Cp_cl = C_plant @ np.hstack([
        np.eye(C_plant.shape[1]),
        np.zeros((
            C_plant.shape[1],
            kp_cl.n_states_out_ - Bc.shape[0] - C_plant.shape[1],
        )),
    ])
    U_test_cl = np.block([
        [Ac, -Bc @ Cp_cl, Bc,
         np.zeros((Bc.shape[0], Bp_cl.shape[1]))],
        [Bp_cl @ Cc, Ap_cl - Bp_cl @ Dc @ Cp_cl, Bp_cl @ Dc, Bp_cl],
    ])
    # Create a closed-loop pipeline with the new Koopman matrix
    kp_test_cl = cl_koopman_pipeline.ClKoopmanPipeline(
        lifting_functions=lifting_functions,
        regressor=pykoop.DataRegressor(coef=U_test_cl.T),
        controller=exp_test['closed_loop']['controller'],
        C_plant=exp_test['closed_loop']['C_plant'],
    )
    # Still fit using ``exp_train`` data, but fit will not change the Koopman
    # matrix set in ``DataRegressor``, it will just check the dimensions and
    # fit the lifting functions.
    kp_test_cl.fit(
        exp_train['closed_loop']['X_train'],
        n_inputs=exp_train['closed_loop']['n_inputs'],
        episode_feature=exp_train['closed_loop']['episode_feature'],
    )
    # Calculate a closed-loop Koopman matrix by adding a controller to the
    # open-loop model
    Up_ol = kp_ol.regressor_.coef_.T
    Ap_ol = Up_ol[:, :Up_ol.shape[0]]
    Bp_ol = Up_ol[:, Up_ol.shape[0]:]
    Cp_ol = C_plant @ np.hstack([
        np.eye(C_plant.shape[1]),
        np.zeros((
            C_plant.shape[1],
            kp_ol.n_states_out_ - C_plant.shape[1],
        )),
    ])
    U_test_ol = np.block([
        [Ac, -Bc @ Cp_ol, Bc,
         np.zeros((Bc.shape[0], Bp_ol.shape[1]))],
        [Bp_ol @ Cc, Ap_ol - Bp_ol @ Dc @ Cp_ol, Bp_ol @ Dc, Bp_ol],
    ])
    # Create a closed-loop pipeline with the new Koopman matrix
    kp_test_ol = cl_koopman_pipeline.ClKoopmanPipeline(
        lifting_functions=lifting_functions,
        regressor=pykoop.DataRegressor(coef=U_test_ol.T),
        controller=exp_test['closed_loop']['controller'],
        C_plant=exp_test['closed_loop']['C_plant'],
    )
    # Still fit using ``exp_train`` data
    kp_test_ol.fit(
        exp_train['closed_loop']['X_train'],
        n_inputs=exp_train['closed_loop']['n_inputs'],
        episode_feature=exp_train['closed_loop']['episode_feature'],
    )
    # Predict trajectories
    kp_cl.predict_trajectory(exp_train['closed_loop']['X_test'])
    kp_ol.predict_trajectory(exp_train['open_loop']['X_test'])
    kp_test_cl.predict_trajectory(exp_test['closed_loop']['X_test'])
    kp_test_ol.predict_trajectory(exp_test['closed_loop']['X_test'])

    # Save results
    # results_path.parent.mkdir(parents=True, exist_ok=True)
    # joblib.dump(None, results_path)  # TODO
