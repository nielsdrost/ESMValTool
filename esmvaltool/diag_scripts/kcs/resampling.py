"""Resampling.

In the second step (see sections 4.2 and 4.3) we resample the results of
ENS-EC for the selected time periods (from the previous step). This is done
by recombining 5 yr periods of the eight members of ENSEC into new resampled
climates, and selecting combinations that match with the spread in CMIP5.
This provides eight resampled EC-Earth time series for each of the scenarios.

"""

from itertools import product

import pandas as pd
import xarray as xr
import numpy as np

from esmvaltool.diag_scripts.shared import (run_diagnostic,
                                            select_metadata)


def get_input_data(cfg):
    """Load ensembles for each variable and merge."""
    dataset_dicts = cfg['input_data'].values()
    target_model_metadata = select_metadata(
        dataset_dicts, dataset=cfg['target_model']
    )
    dataset = []
    for short_name in "pr", "tas":
        var = select_metadata(target_model_metadata, variable_group=short_name)
        files = [metadata['filename'] for metadata in var]
        dataset.append(
            xr.open_mfdataset(files, concat_dim='ensemble_member', combine='nested')
        )
    return xr.merge(dataset)


def segment(dataset, period, step=5):
    """Segment (part of) a dataset into x blocks of n years each.

    Returns a new dataset with additional coordinate 'segment'.
    """
    segments = []
    for year in range(*period, step):
        segments.append(dataset.sel(time=slice(str(year), str(year + step))))
    segmented_dataset = xr.concat(segments, dim='segment')
    return segmented_dataset


def all_possible_combinations(n_ensemble_members, n_segments):
    """Generate indexer.

    Returns indices for all possible combinations of
    segments and ensembles.
    """
    # Create a DataArray once...
    indices = xr.DataArray(
        data=np.zeros(n_segments),
        dims=['segment'],
        coords={'segment': np.arange(n_segments)})

    # ...and update its values for all possible combinations
    # TODO DataArray does not have update but Dataset.
    for combination in product(range(n_ensemble_members), repeat=n_segments):
        indices.values = list(combination)
        yield indices


def selected_combinations(combinations):
    """Generate indexer for all selected combinations of segmented dataset.

    combinations: a list of combinations
    """
    n_segments = len(combinations[0])

    # Create a DataArray once...
    indices = xr.DataArray(
        data=np.zeros(n_segments),
        dims=['segment'],
        coords={'segment': np.arange(n_segments)})

    # ...and update its values for each selected combinations
    for combination in combinations:
        indices.values = combination
        yield indices


def most_promising_combinations(segment_means, target, n_ensemble_members, n=1000):
    """Get n out of all possible combinations that are closest to the target."""
    promising_combinations = []
    for combination in all_possible_combinations(n_ensemble_members, n_segments=6):
        recombined_segment_means = segment_means.sel(
            ensemble_member=combination)
        new_overall_mean = recombined_segment_means.mean('segment')
        distance_to_target = abs(new_overall_mean - target)
        promising_combinations.append([combination.values, distance_to_target])
    top_all = pd.DataFrame(promising_combinations,
                           columns=['combination', 'distance_to_target'])
    return top_all.sort_values('distance_to_target').head(n)


def within_bounds(values, bounds):
    """Return true if value is within percentile bounds."""
    low, high = np.percentile(values, bounds)
    return values.between(low, high)


def determine_penalties(overlap):
    """Determine penalties dependent on the number of overlaps."""
    return np.piecewise(overlap,
                        condlist=[overlap < 3, overlap == 3, overlap == 4, overlap > 4],
                        funclist=[0, 1, 5, 100])


def select_final_subset(combinations, n_sample=8):
    """Find n samples with minimal reuse of ensemble members per segment.

    combinations: a pandas series with the remaining candidates
    n: the final number of samples drawn from the remaining set.
    """
    # Convert series of tuples to 2d numpy array (much faster!)
    combinations = np.array(
        [list(combination) for combination in combinations]
    )

    # Store the indices in a nice dataframe
    _, n_segments = combinations.shape
    best_combination = pd.DataFrame(
        data=None,
        columns=[f'Segment {x}' for x in range(n_segments)],
        index=[f'Combination {x}' for x in range(n_sample)]
    )

    # Random number generator
    rng = np.random.default_rng()

    lowest_penalty = 500  # just a random high value
    # TODO check the loop because i is unused!
    for i in range(10000):
        sample = rng.choice(combinations, size=n_sample)
        penalty = 0
        for segment in sample.T:
            _, counts = np.unique(segment, return_counts=True)
            penalty += determine_penalties(counts).sum()
        if penalty < lowest_penalty:
            lowest_penalty = penalty
            best_combination.loc[:, :] = sample

    return best_combination


def main(cfg):
    """Resample the model of interest.

    Step 0: Read the data, extract segmented subsets for both the control
    and future periods, and precompute seasonal means for each segment.

    Step 1a: Get 1000 combinations for the control period.
    These samples should have the same mean winter precipitation as the
    overall mean of the x ensemble members.

    Step 1b: Get 1000 combinations for the future period.
    Now, for future period, the target value is a relative change with respect to
    the overall mean of the control period.

    Step 2a: For each set of 1000 samples from 1a-b, compute summer precipitation,
    and summer and winter temperature.

    Step 2b: For each scenario, select samples for which summer precipitation,
    and summer and winter temperature are within the percentile bounds specified
    in the recipe.

    Step 3: Select final set of eight samples with minimal reuse of the same
    ensemble member for the same period.
    From 10.000 randomly selected sets of 8 samples, count
    and penalize re-used segments (1 for 3*reuse, 5 for 4*reuse).
    Chose the set with the lowest penalty.
    """
    # Step 0:
    # Read the data
    dataset = get_input_data(cfg)
    n_ensemble_members = len(dataset.ensemble_member)

    # Precompute the segment season means for the control period ...
    segments_season_means = {}
    segments = segment(dataset, cfg['control_period'], step=5)
    season_means = segments.groupby('time.season').mean()
    segments_season_means['control'] = season_means

    # ... and for all the future scenarios
    for scenario, info in cfg['scenarios'].items():
        segments = segment(dataset, info['resampling_period'], step=5)
        season_means = segments.groupby('time.season').mean()
        segments_season_means[scenario] = season_means

    # Step 1:
    # Create a dictionary to store the selected indices later on.
    selected_indices = {}

    # Step 1a: Get 1000 combinations for the control period
    # Get mean winter precipitation per segment
    winter_mean_pr_segments = segments_season_means['control'].pr.sel(
        season='DJF')
    # Get overall mean of segments
    target = winter_mean_pr_segments.mean()
    # Select top1000 values
    selected_indices['control'] = most_promising_combinations(
        winter_mean_pr_segments, target, n_ensemble_members
    )

    # Step 1b: Get 1000 combinations for the future period
    control_period_winter_mean_pr = target
    for scenario, info in cfg['scenarios'].items():
        # The target value is a relative change with respect to
        # the overall mean of the control period
        target = control_period_winter_mean_pr * (1 + info['dpr_winter'] / 100)
        winter_mean_pr_segments = segments_season_means[scenario].pr.sel(
            season='DJF')
        selected_indices[scenario] = most_promising_combinations(
            winter_mean_pr_segments, target, n_ensemble_members
        )

    # Step 2a: For each set of 1000 samples from 1a-b, compute summer pr,
    # and summer and winter tas.
    for name, dataframe in selected_indices.items():
        top1000 = dataframe.combination
        segment_means = segments_season_means[name]

        filter2_variables = []
        columns = ['combination', 'pr_summer', 'tas_winter', 'tas_summer']
        for combination in selected_combinations(top1000):
            recombined_segment_means = segment_means.sel(
                ensemble_member=combination)
            new_overall_season_means = recombined_segment_means.mean('segment')

            filter2_variables.append([
                combination.values,
                new_overall_season_means.pr.sel(season='JJA'),
                new_overall_season_means.tas.sel(season='DJF'),
                new_overall_season_means.tas.sel(season='JJA')
            ])

        selected_indices[name] = pd.DataFrame(filter2_variables, columns=columns)

    # Step 2b: For each scenario, select samples for which summer pr, and
    # summer and winter tas are within the percentile bounds specified
    # in the recipe
    top1000_control = selected_indices['control']
    for scenario, info in cfg['scenarios'].items():
        top1000 = selected_indices[scenario]

        # Get relatively high/low values for the control period ...
        pr_summer = within_bounds(
            top1000_control['pr_summer'], info['pr_summer_control'])
        tas_winter = within_bounds(
            top1000_control['tas_winter'], info['tas_winter_control'])
        tas_summer = within_bounds(
            top1000_control['tas_summer'], info['tas_summer_control'])
        subset_control = top1000_control[
            np.all([pr_summer, tas_winter, tas_summer], axis=0)
        ]

        # ... combined with relatively high/low values for the future period
        pr_summer = within_bounds(
            top1000['pr_summer'], info['pr_summer_future'])
        tas_winter = within_bounds(
            top1000['tas_winter'], info['tas_winter_future'])
        tas_summer = within_bounds(
            top1000['tas_summer'], info['tas_summer_future'])
        subset_future = top1000[np.all([pr_summer, tas_winter, tas_summer], axis=0)]

        selected_indices[scenario] = {
            'control': subset_control,
            'future': subset_future
        }

        del selected_indices['control']  # No longer needed

    # Step 3: Select final set of eight samples with minimal reuse of the same
    # ensemble member for the same period.
    for scenario, dataframes in selected_indices.items():
        scenario_output_tables = []
        for period in ['control', 'future']:
            remaining_combinations = dataframes[period].combination
            result = select_final_subset(remaining_combinations)
            scenario_output_tables.append(result)

        scenario_output_tables = pd.concat(
            scenario_output_tables, axis=1, keys=['control', 'future'])
        print(f"Selected recombinations for scenario {scenario}:")
        print(scenario_output_tables)
        filename = f'indices_{scenario}.csv'
        scenario_output_tables.to_csv(filename)


if __name__ == '__main__':
    with run_diagnostic() as config:
        main(config)