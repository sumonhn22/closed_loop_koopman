"""Open-loop Optuna study."""

import argparse

import joblib
import numpy as np
import optuna
import pykoop
import sklearn.model_selection

import cl_koopman_pipeline


def main():
    """Run an open-loop Optuna study."""
    # Parse arguments
    parser = argparse.ArgumentParser()
    parser.add_argument(
        'experiment_path',
        type=str,
    )
    parser.add_argument(
        'lifting_functions_path',
        type=str,
    )
    parser.add_argument(
        'study_path',
        type=str,
    )
    parser.add_argument(
        'sklearn_split_seed',
        type=int,
    )
    args = parser.parse_args()
    # Load data
    dataset = joblib.load(args.experiment_path)
    # Load lifting functions
    lifting_functions = joblib.load(args.lifting_functions_path)

    def objective(trial: optuna.Trial) -> float:
        """Implement open-loop objective function."""
        # Split data
        gss = sklearn.model_selection.GroupShuffleSplit(
            n_splits=3,
            test_size=0.2,
            random_state=args.sklearn_split_seed,
        )
        gss_iter = gss.split(
            dataset['open_loop']['X_train'],
            groups=dataset['open_loop']['X_train'][:, 0],
        )
        # Run cross-validation
        r2 = []
        for i, (train_index, test_index) in enumerate(gss_iter):
            # Get hyperparameters from Optuna (true range set in ``dodo.py``)
            alpha = trial.suggest_float('alpha', low=1e-12, high=1e12)
            # Train-test split
            X_train_ol_i = dataset['open_loop']['X_train'][train_index, :]
            X_train_cl_i = dataset['closed_loop']['X_train'][train_index, :]
            X_test_cl_i = dataset['closed_loop']['X_train'][test_index, :]
            # Create pipeline
            kp_ol = pykoop.KoopmanPipeline(
                lifting_functions=[(
                    'split',
                    pykoop.SplitPipeline(
                        lifting_functions_state=lifting_functions,
                        lifting_functions_input=None,
                    ),
                )],
                regressor=pykoop.Edmd(alpha=alpha),
            )
            # Fit model
            kp_ol.fit(
                X_train_ol_i,
                n_inputs=dataset['open_loop']['n_inputs'],
                episode_feature=dataset['open_loop']['episode_feature'],
            )
            # Get closed-loop model
            kp_cl = cl_koopman_pipeline.ClKoopmanPipeline.from_ol_pipeline(
                kp_ol,
                controller=dataset['closed_loop']['controller'],
                C_plant=dataset['closed_loop']['C_plant'],
            )
            kp_cl.fit(
                X_train_cl_i,
                n_inputs=dataset['closed_loop']['n_inputs'],
                episode_feature=dataset['closed_loop']['episode_feature'],
            )
            # Predict open-loop trajectory
            with pykoop.config_context(skip_validation=True):
                X_pred = kp_cl.predict_trajectory(X_test_cl_i)
            # Score open-loop trajectory
            r2_i = pykoop.score_trajectory(
                X_pred,
                X_test_cl_i[:, :X_pred.shape[1]],
                regression_metric='r2',
                episode_feature=dataset['closed_loop']['episode_feature'],
            )
            r2.append(r2_i)
            trial.report(r2_i, step=i)
            # Check if trial should be pruned
            if trial.should_prune():
                raise optuna.TrialPruned()
        return np.mean(r2)

    study = optuna.load_study(
        study_name=None,
        storage=args.study_path,
    )
    study.optimize(
        objective,
    )


if __name__ == '__main__':
    main()
