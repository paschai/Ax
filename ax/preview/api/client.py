# Copyright (c) Meta Platforms, Inc. and affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

# pyre-strict

from logging import Logger
from typing import Sequence

import numpy as np

from ax.analysis.analysis import Analysis, AnalysisCard  # Used as a return type
from ax.analysis.markdown.markdown_analysis import (
    markdown_analysis_card_from_analysis_e,
)
from ax.analysis.utils import choose_analyses

from ax.core.base_trial import TrialStatus  # Used as a return type

from ax.core.experiment import Experiment
from ax.core.metric import Metric
from ax.core.objective import MultiObjective, Objective, ScalarizedObjective
from ax.core.observation import ObservationFeatures
from ax.core.optimization_config import OptimizationConfig
from ax.core.runner import Runner
from ax.core.trial import Trial
from ax.core.utils import get_pending_observation_features_based_on_trial_status
from ax.early_stopping.strategies import BaseEarlyStoppingStrategy
from ax.exceptions.core import UnsupportedError, UserInputError
from ax.modelbridge.dispatch_utils import choose_generation_strategy
from ax.modelbridge.generation_strategy import GenerationStrategy

from ax.preview.api.configs import (
    DatabaseConfig,
    ExperimentConfig,
    GenerationStrategyConfig,
    OrchestrationConfig,
)
from ax.preview.api.protocols.metric import IMetric
from ax.preview.api.protocols.runner import IRunner
from ax.preview.api.types import TOutcome, TParameterization
from ax.preview.api.utils.instantiation.from_config import experiment_from_config
from ax.preview.api.utils.instantiation.from_string import (
    optimization_config_from_string,
)
from ax.service.scheduler import Scheduler, SchedulerOptions
from ax.utils.common.logger import get_logger
from ax.utils.common.random import with_rng_seed
from pyre_extensions import assert_is_instance, none_throws
from typing_extensions import Self

logger: Logger = get_logger(__name__)


class Client:
    _experiment: Experiment | None = None
    _generation_strategy: GenerationStrategy | None = None
    _early_stopping_strategy: BaseEarlyStoppingStrategy | None = None

    def __init__(
        self,
        db_config: DatabaseConfig | None = None,
        random_seed: int | None = None,
    ) -> None:
        """
        Initialize a Client, which manages state across the lifecycle of an experiment.

        Args:
            db_config: Configuration for saving to and loading from a database. If
                elided the experiment will not automatically be saved to a database.
            random_seed: An optional integer to set the random seed for reproducibility
                of the experiment's results. If not provided, the random seed will not
                be set, leading to potentially different results on different runs.
        """
        self._db_config = db_config
        self._random_seed = random_seed

    # -------------------- Section 1: Configure --------------------------------------
    def configure_experiment(self, experiment_config: ExperimentConfig) -> None:
        """
        Given an ExperimentConfig, construct the Ax Experiment object. Note that
        validation occurs at time of config instantiation, not at
        configure_experiment.

        This method only constitutes defining the search space and misc. metadata
        like name, description, and owners.

        Saves to database on completion if db_config is present.
        """
        if self._experiment is not None:
            raise UnsupportedError(
                "Experiment already configured. Please create a new Client if you "
                "would like a new experiment."
            )

        self._experiment = experiment_from_config(config=experiment_config)

        if self._db_config is not None:
            # TODO[mpolson64] Save to database
            ...

    def configure_optimization(
        self,
        # Objective string is an expression and allows us to express single,
        # scalarized, and multi-objective via SymPy parsing.
        # Ex: "loss", "ne1 + ne1", "-ne, qps"
        objective: str,
        # Outcome constraints will also be parsed via SymPy
        # Ex: "num_layers1 <= num_layers2", "compound_a + compound_b <= 1"
        # To indicate a relative constraint multiply your bound by "baseline"
        # Ex: "qps >= 0.95 * baseline" will constrain such that the QPS is at least
        # 95% of the baseline arm's QPS.
        outcome_constraints: Sequence[str] | None = None,
    ) -> None:
        """
        Configures the goals of the optimization by setting the OptimizationConfig.
        Metrics referenced here by their name will be moved from the Experiment's
        tracking_metrics if they were were already present (i.e. they were attached via
        configure_metrics) or added as base Metrics.

        Args:
            objective: Objective is a string and allows us to express single,
                scalarized, and multi-objective goals. Ex: "loss", "ne1 + ne1",
                "-ne, qps"
            outcome_constraints: Outcome constraints are also strings and allow us to
                express a desire to have a metric clear a threshold but not be
                further optimized. These constraints are expressed as inequalities.
                Ex: "qps >= 100", "0.5 * ne1 + 0.5 * ne2 >= 0.95".
                To indicate a relative constraint multiply your bound by "baseline"
                Ex: "qps >= 0.95 * baseline" will constrain such that the QPS is at
                least 95% of the baseline arm's QPS.
                Note that scalarized outcome constraints cannot be relative.


        Saves to database on completion if db_config is present.
        """

        self._none_throws_experiment().optimization_config = (
            optimization_config_from_string(
                objective_str=objective,
                outcome_constraint_strs=outcome_constraints,
            )
        )

        if self._db_config is not None:
            # TODO[mpolson64] Save to database
            ...

    def configure_generation_strategy(
        self, generation_strategy_config: GenerationStrategyConfig
    ) -> None:
        """
        Overwrite the existing GenerationStrategy by calling choose_gs using the
        arguments of the GenerationStrategyConfig as parameters.

        Saves to database on completion if db_config is present.
        """

        generation_strategy = choose_generation_strategy(
            search_space=self._none_throws_experiment().search_space,
            optimization_config=self._none_throws_experiment().optimization_config,
            num_initialization_trials=(
                generation_strategy_config.initialization_budget
            ),
            min_sobol_trials_observed=(
                generation_strategy_config.min_observed_initialization_trials
            ),
            enforce_sequential_optimization=(
                not generation_strategy_config.allow_exceeding_initialization_budget
            ),
            random_seed=generation_strategy_config.initialization_random_seed,
        )

        # Necessary for storage implications, may be removed in the future
        generation_strategy._experiment = self._none_throws_experiment()

        self._generation_strategy = generation_strategy

        if self._db_config is not None:
            # TODO[mpolson64] Save to database
            ...

    # -------------------- Section 1.1: Configure Automation ------------------------
    def configure_runner(self, runner: IRunner) -> None:
        """
        Attaches a Runner to the Experiment.

        Saves to database on completion if db_config is present.
        """
        self._set_runner(runner=runner)

    def configure_metrics(self, metrics: Sequence[IMetric]) -> None:
        """
        Attach a class with logic for autmating fetching of a given metric by
        replacing its instance with the provided Metric from metrics sequence input,
        or adds the Metric provided to the Experiment as a tracking metric if that
        metric was not already present.
        """
        self._set_metrics(metrics=metrics)

    # -------------------- Section 1.2: Set (not API) -------------------------------
    def set_experiment(self, experiment: Experiment) -> None:
        """
        This method is not part of the API and is provided (without guarantees of
        method signature stability) for the convenience of some developers, power
        users, and partners.

        Overwrite the existing Experiment with the provided Experiment.

        Saves to database on completion if db_config is present.
        """
        self._experiment = experiment

        if self._db_config is not None:
            # TODO[mpolson64] Save to database
            ...

    def set_optimization_config(self, optimization_config: OptimizationConfig) -> None:
        """
        This method is not part of the API and is provided (without guarantees of
        method signature stability) for the convenience of some developers, power
        users, and partners.

        Overwrite the existing OptimizationConfig with the provided OptimizationConfig.

        Saves to database on completion if db_config is present.
        """
        self._none_throws_experiment().optimization_config = optimization_config

        if self._db_config is not None:
            # TODO[mpolson64] Save to database
            ...

    def set_generation_strategy(self, generation_strategy: GenerationStrategy) -> None:
        """
        This method is not part of the API and is provided (without guarantees of
        method signature stability) for the convenience of some developers, power
        users, and partners.

        Overwrite the existing GenerationStrategy with the provided GenerationStrategy.

        Saves to database on completion if db_config is present.
        """
        self._generation_strategy = generation_strategy
        none_throws(
            self._generation_strategy
        )._experiment = self._none_throws_experiment()

        none_throws(
            self._generation_strategy
        )._experiment = self._none_throws_experiment()

        if self._db_config is not None:
            # TODO[mpolson64] Save to database
            ...

    def set_early_stopping_strategy(
        self, early_stopping_strategy: BaseEarlyStoppingStrategy
    ) -> None:
        """
        This method is not part of the API and is provided (without guarantees of
        method signature stability) for the convenience of some developers, power
        users, and partners.

        Overwrite the existing EarlyStoppingStrategy with the provided
        EarlyStoppingStrategy.

        Saves to database on completion if db_config is present.
        """
        self._early_stopping_strategy = early_stopping_strategy

        if self._db_config is not None:
            # TODO[mpolson64] Save to database
            ...

    def _set_runner(self, runner: Runner) -> None:
        """
        This method is not part of the API and is provided (without guarantees of
        method signature stability) for the convenience of some developers and power
        users.

        Attaches a Runner to the Experiment.

        Saves to database on completion if db_config is present.
        """
        self._none_throws_experiment().runner = runner

        if self._db_config is not None:
            # TODO[mpolson64] Save to database
            ...

    def _set_metrics(self, metrics: Sequence[Metric]) -> None:
        """
        This method is not part of the API and is provided (without guarantees of
        method signature stability) for the convenience of some developers, power
        users, and partners.

        Attach a class with logic for autmating fetching of a given metric by
        replacing its instance with the provided Metric from metrics sequence input,
        or adds the Metric provided to the Experiment as a tracking metric if that
        metric was not already present.
        """
        # If an equivalently named Metric already exists on the Experiment, replace it
        # with the Metric provided. Otherwise, add the Metric to the Experiment as a
        # tracking metric.
        for metric in metrics:
            # Check the optimization config first
            self._overwrite_metric(metric=metric)

        if self._db_config is not None:
            # TODO[mpolson64] Save to database
            ...

    # -------------------- Section 2. Conduct Experiment ----------------------------
    def get_next_trials(
        self, maximum_trials: int = 1, fixed_parameters: TParameterization | None = None
    ) -> dict[int, TParameterization]:
        """
        Create up to `maximum_trials` trials using the `GenerationStrategy`, attach
        them to the Experiment, with status RUNNING, and return a mapping of trial
        index to its parameterization. If a partial parameterization is provided via
        fixed_parameters those parameters will be locked for all trials.

        This will need to be rethought somewhat when we add support for BatchTrials,
        but will be okay for current supported functionality.

        Saves to database on completion if db_config is present.

        Returns:
            A mapping of trial index to parameterization.
        """

        if self._none_throws_experiment().optimization_config is None:
            raise UnsupportedError(
                "OptimizationConfig not set. Please call configure_optimization before "
                "generating trials."
            )

        trials: list[Trial] = []
        with with_rng_seed(seed=self._random_seed):
            gs = self._generation_strategy_or_choose()

            # This will be changed to use gen directly post gen-unfication cc @mgarrard
            generator_runs = gs.gen_for_multiple_trials_with_multiple_models(
                experiment=self._none_throws_experiment(),
                pending_observations=(
                    get_pending_observation_features_based_on_trial_status(
                        experiment=self._none_throws_experiment()
                    )
                ),
                n=1,
                fixed_features=(
                    # pyre-fixme[6]: Type narrowing broken because core Ax
                    # TParameterization is dict not Mapping
                    ObservationFeatures(parameters=fixed_parameters)
                    if fixed_parameters is not None
                    else None
                ),
                num_trials=maximum_trials,
            )

        for generator_run in generator_runs:
            trial = assert_is_instance(
                self._none_throws_experiment().new_trial(
                    generator_run=generator_run[0],
                ),
                Trial,
            )
            trial.mark_running(no_runner_required=True)

            trials.append(trial)

        if self._db_config is not None:
            # TODO[mpolson64] Save trial and update generation strategy
            ...

        # pyre-fixme[7]: Core Ax allows users to specify TParameterization values as
        # None, but we do not allow this in the API.
        return {trial.index: none_throws(trial.arm).parameters for trial in trials}

    def complete_trial(
        self,
        trial_index: int,
        raw_data: TOutcome | None = None,
        progression: int | None = None,
    ) -> TrialStatus:
        """
        Indicate the trial is complete while optionally attach data. In non-timeseries
        settings users should prefer to use complete_trial with raw_data over
        attach_data. Ax will determine the trial's status automatically:
            - If all metrics on the OptimizationConfig are present the trial will be
                marked as COMPLETED
            - If any metrics on the OptimizationConfig are missing the trial will be
                marked as FAILED

        Saves to database on completion if db_config is present.
        """
        if raw_data is not None:
            self.attach_data(
                trial_index=trial_index, raw_data=raw_data, progression=progression
            )

        experiment = self._none_throws_experiment()

        # If no OptimizationConfig is set, mark the trial as COMPLETED
        if (optimization_config := experiment.optimization_config) is None:
            experiment.trials[trial_index].mark_completed()
        else:
            trial_data = experiment.lookup_data(trial_indices=[trial_index])
            missing_metrics = {*optimization_config.metrics.keys()} - {
                *trial_data.metric_names
            }

            # If all necessary metrics are present mark the trial as COMPLETED
            if len(missing_metrics) == 0:
                experiment.trials[trial_index].mark_completed()

            # If any metrics are missing mark the trial as FAILED
            else:
                logger.warning(
                    f"Trial {trial_index} marked completed but metrics "
                    f"{missing_metrics} are missing, marking trial FAILED."
                )
                self.mark_trial_failed(trial_index=trial_index)

        if self._db_config is not None:
            # TODO[mpolson64] Save trial
            ...

        return experiment.trials[trial_index].status

    def attach_data(
        self,
        trial_index: int,
        raw_data: TOutcome,
        progression: int | None = None,
    ) -> None:
        """
        Attach data without indicating the trial is complete. Missing metrics are,
        allowed, and unexpected metric values will be added to the Experiment as
        tracking metrics. If progression is provided the Experiment will be updated to
        use MapData and the data will be attached to the appropriate step.

        Saves to database on completion if db_config is present.
        """

        # If no progression is provided assume the data is not timeseries-like and
        # set step=NaN
        data_with_progression = [
            ({"step": progression if progression is not None else np.nan}, raw_data)
        ]

        trial = assert_is_instance(
            self._none_throws_experiment().trials[trial_index], Trial
        )
        trial.update_trial_data(
            # pyre-fixme[6]: Type narrowing broken because core Ax TParameterization
            # is dict not Mapping
            raw_data=data_with_progression,
            combine_with_last_data=True,
        )

        if self._db_config is not None:
            # TODO[mpolson64] Save trial
            ...

    # -------------------- Section 2.1 Custom trials --------------------------------
    def attach_trial(
        self, parameters: TParameterization, arm_name: str | None = None
    ) -> int:
        """
        Attach a single-arm trial to the experiment with the provided parameters.
        The trial will be marked as RUNNING and must be completed manually by the
        user.

        Saves to database on completion if db_config is present.

        Returns:
            The index of the attached trial.
        """
        _, trial_index = self._none_throws_experiment().attach_trial(
            # pyre-fixme[6]: Type narrowing broken because core Ax TParameterization
            # is dict not Mapping
            parameterizations=[parameters],
            arm_names=[arm_name] if arm_name else None,
        )

        if self._db_config is not None:
            # TODO[mpolson64] Save trial
            ...

        return trial_index

    def attach_baseline(
        self, parameters: TParameterization, arm_name: str | None = None
    ) -> int:
        """
        Attaches custom single-arm trial to an experiment specifically for use as the
        baseline or status quo in evaluating relative outcome constraints and
        improvement over baseline objective value. The trial will be marked as RUNNING
        and must be completed manually by the user.

        Returns:
            The index of the attached trial.

        Saves to database on completion if db_config is present.
        """
        trial_index = self.attach_trial(
            parameters=parameters,
            arm_name=arm_name or "baseline",
        )

        self._none_throws_experiment().status_quo = assert_is_instance(
            self._none_throws_experiment().trials[trial_index], Trial
        ).arm

        if self._db_config is not None:
            ...

        return trial_index

    # -------------------- Section 2.2 Early Stopping -------------------------------
    def should_stop_trial_early(self, trial_index: int) -> bool:
        """
        Check if the trial should be stopped early. If True and the user wishes to heed
        Ax's recommendation the user should manually stop the trial and call
        mark_trial_early_stopped(trial_index). The EarlyStoppingStrategy may be selected
        automatically or set manually via set_early_stopping_strategy.

        Returns:
            Whether the trial should be stopped early.
        """
        if self._early_stopping_strategy is None:
            # In the future we may want to support inferring a default early stopping
            # strategy
            raise UnsupportedError(
                "Early stopping strategy not set. Please set an early stopping "
                "strategy before calling should_stop_trial_early."
            )

        es_response = none_throws(
            self._early_stopping_strategy
        ).should_stop_trials_early(
            trial_indices={trial_index}, experiment=self._none_throws_experiment()
        )

        # TODO[mpolson64]: log the returned reason for stopping the trial
        return trial_index in es_response

    # -------------------- Section 2.3 Marking trial status manually ----------------
    def mark_trial_failed(self, trial_index: int) -> None:
        """
        Manually mark a trial as FAILED. FAILED trials typically may be re-suggested by
        get_next_trials, though this is controlled by the GenerationStrategy.

        Saves to database on completion if db_config is present.
        """
        self._none_throws_experiment().trials[trial_index].mark_failed()

        if self._db_config is not None:
            # TODO[mpolson64] Save to database
            ...

    def mark_trial_abandoned(self, trial_index: int) -> None:
        """
        Manually mark a trial as ABANDONED. ABANDONED trials are typically not able to
        be re-suggested by get_next_trials, though this is controlled by the
        GenerationStrategy.

        Saves to database on completion if db_config is present.
        """
        self._none_throws_experiment().trials[trial_index].mark_abandoned()

        if self._db_config is not None:
            # TODO[mpolson64] Save to database
            ...

    def mark_trial_early_stopped(
        self, trial_index: int, raw_data: TOutcome, progression: int | None = None
    ) -> None:
        """
        Manually mark a trial as EARLY_STOPPED while attaching the most recent data.
        This is used when the user has decided (with or without Ax's recommendation) to
        stop the trial early. EARLY_STOPPED trials will not be re-suggested by
        get_next_trials.

        Saves to database on completion if db_config is present.
        """

        # First attach the new data
        self.attach_data(
            trial_index=trial_index, raw_data=raw_data, progression=progression
        )

        self._none_throws_experiment().trials[trial_index].mark_early_stopped()

        if self._db_config is not None:
            # TODO[mpolson64] Save to database
            ...

    def run_trials(self, maximum_trials: int, options: OrchestrationConfig) -> None:
        """
        Run maximum_trials trials in a loop by creating an ephemeral Scheduler under
        the hood using the Experiment, GenerationStrategy, Metrics, and Runner attached
        to this AxClient along with the provided OrchestrationConfig.

        Saves to database on completion if db_config is present.
        """

        scheduler = Scheduler(
            experiment=self._none_throws_experiment(),
            generation_strategy=(self._generation_strategy_or_choose()),
            options=SchedulerOptions(
                max_pending_trials=options.parallelism,
                tolerated_trial_failure_rate=options.tolerated_trial_failure_rate,
                init_seconds_between_polls=options.initial_seconds_between_polls,
            ),
            # TODO[mpolson64] Add db_settings=self._db_config when adding storage
        )

        # Note: This scheduler call will handle storage internally
        scheduler.run_n_trials(max_trials=maximum_trials)

    # -------------------- Section 3. Analyze ---------------------------------------
    def compute_analyses(
        self,
        analyses: Sequence[Analysis] | None = None,
    ) -> list[AnalysisCard]:
        """
        Compute AnalysisCards (data about the optimization for end-user consumption)
        using the Experiment and GenerationStrategy. If no analyses are provided use
        some heuristic to determine which analyses to run. If some analyses fail, log
        failure and continue to compute the rest.

        Note that the Analysis class is NOT part of the API and its methods are subject
        to change incompatibly between minor versions. Users are encouraged to use the
        provided analyses or leave this argument as None to use the default analyses.

        Saves to database on completion if db_config is present.

        Returns:
            A list of AnalysisCards.
        """

        analyses = (
            analyses
            if analyses is not None
            else choose_analyses(experiment=self._none_throws_experiment())
        )

        # Compute Analyses one by one and accumulate Results holding either the
        # AnalysisCard or an Exception and some metadata
        results = [
            analysis.compute_result(
                experiment=self._none_throws_experiment(),
                generation_strategy=self._generation_strategy_or_choose(),
            )
            for analysis in analyses
        ]

        # Turn Exceptions into MarkdownAnalysisCards with the traceback as the message
        cards = [
            result.unwrap_or_else(markdown_analysis_card_from_analysis_e)
            for result in results
        ]

        if self._db_config is not None:
            # TODO[mpolson64] Save cards to database
            ...

        return cards

    def get_best_trial(
        self, use_model_predictions: bool = True
    ) -> tuple[int, TParameterization, TOutcome]:
        """
        Calculates the best in-sample trial.

        Returns:
            - The index of the best trial
            - The parameters of the best trial
            - The metric values associated withthe best trial
        """
        ...

    def get_pareto_frontier(
        self, use_model_predictions: bool = True
    ) -> dict[int, tuple[TParameterization, TOutcome]]:
        """
        Calculates the in-sample Pareto frontier.

        Returns:
            A mapping of trial index to its parameterization and metric values.
        """
        ...

    def predict(
        self,
        points: Sequence[TParameterization],
    ) -> list[TOutcome]:
        """
        Use the GenerationStrategy to predict the outcome of the provided
        list of parameterizations.

        Returns:
            A list of mappings from metric name to predicted mean and SEM
        """
        search_space = self._none_throws_experiment().search_space
        for parameters in points:
            search_space.check_membership(
                # pyre-fixme[6]: Core Ax allows users to specify TParameterization
                # values as None but we do not allow this in the API.
                parameterization=parameters,
                raise_error=True,
                check_all_parameters_present=True,
            )

        generation_strategy = self._generation_strategy_or_choose()

        try:
            mean, covariance = none_throws(generation_strategy.model).predict(
                observation_features=[
                    # pyre-fixme[6]: Core Ax allows users to specify TParameterization
                    # values as None but we do not allow this in the API.
                    ObservationFeatures(parameters=parameters)
                    for parameters in points
                ]
            )
        except (UserInputError, AssertionError) as e:
            raise UnsupportedError(
                "Predicting with the GenerationStrategy's modelbridge failed. This "
                "could be because the current GenerationNode is not predictive -- try "
                "running more trials to progress to a predictive GenerationNode."
            ) from e

        return [
            {
                metric_name: (
                    mean[metric_name][i],
                    covariance[metric_name][metric_name][i],
                )
                for metric_name in mean.keys()
            }
            for i in range(len(points))
        ]

    # -------------------- Section 4: Save/Load -------------------------------------
    # Note: SQL storage handled automatically during regular usage
    def save_to_json_file(self, filepath: str = "ax_client_snapshot.json") -> None:
        """
        Save a JSON-serialized snapshot of this `AxClient`'s settings and state
        to a .json file by the given path.
        """
        ...

    @classmethod
    def load_from_json_file(
        cls,
        filepath: str = "ax_client_snapshot.json",
    ) -> Self:
        """
        Restore an `AxClient` and its state from a JSON-serialized snapshot,
        residing in a .json file by the given path.

        Returns:
            The restored `AxClient`.
        """
        ...

    def load_from_database(
        self,
        experiment_name: str,
    ) -> Self:
        """
        Restore an `AxClient` and its state from database by the given name.

        Returns:
            The restored `AxClient`.
        """
        ...

    def _none_throws_experiment(self) -> Experiment:
        return none_throws(
            self._experiment,
            (
                "Experiment not set. Please call configure_experiment or load an "
                "experiment before utilizing any other methods on the Client."
            ),
        )

    def _generation_strategy_or_choose(
        self,
    ) -> GenerationStrategy:
        """
        If a GenerationStrategy is not set, choose a default one (save to database) and
        return it.
        """

        if self._generation_strategy is None:
            self.configure_generation_strategy(
                generation_strategy_config=GenerationStrategyConfig()
            )

        return none_throws(self._generation_strategy)

    def _overwrite_metric(self, metric: Metric) -> None:
        """
        Overwrite an existing Metric on the Experiment with the provided Metric if they
        share the same name. If not Metric with the same name exists, add the Metric as
        a tracking metric.

        Note that this method does not save the Experiment to the database (this is
        handled in self._set_metrics).
        """

        # Check the OptimizationConfig first
        if (
            optimization_config := self._none_throws_experiment().optimization_config
        ) is not None:
            # Check the objective
            if isinstance(
                multi_objective := optimization_config.objective, MultiObjective
            ):
                for i in range(len(multi_objective.objectives)):
                    if metric.name == multi_objective.objectives[i].metric.name:
                        multi_objective._objectives[i]._metric = metric
                        return

            if isinstance(
                scalarized_objective := optimization_config.objective,
                ScalarizedObjective,
            ):
                for i in range(len(scalarized_objective.metrics)):
                    if metric.name == scalarized_objective.metrics[i].name:
                        scalarized_objective._metrics[i] = metric
                        return

            if (
                isinstance(optimization_config.objective, Objective)
                and metric.name == optimization_config.objective.metric.name
            ):
                optimization_config.objective._metric = metric
                return

            # Check the outcome constraints
            for i in range(len(optimization_config.outcome_constraints)):
                if (
                    metric.name
                    == optimization_config.outcome_constraints[i].metric.name
                ):
                    optimization_config._outcome_constraints[i]._metric = metric
                    return

        # Check the tracking metrics
        tracking_metric_names = self._none_throws_experiment()._tracking_metrics.keys()
        if metric.name in tracking_metric_names:
            self._none_throws_experiment()._tracking_metrics[metric.name] = metric
            return

        # If an equivalently named Metric does not exist, add it as a tracking
        # metric.
        self._none_throws_experiment().add_tracking_metric(metric=metric)
        logger.warning(
            f"Metric {metric} not found in optimization config, added as tracking "
            "metric."
        )