import os
import time
import importlib

from joblib import Parallel, delayed
import pandas as pd

from cowidev.vax.batch import __all__ as batch_countries
from cowidev.vax.incremental import __all__ as incremental_countries
from cowidev.utils.log import get_logger, print_eoe
from cowidev.utils import paths
from cowidev.utils.clean.dates import localdate
from cowidev.utils.s3 import df_from_s3, df_to_s3


# Logger
logger = get_logger()

# Import modules
country_to_module_batch = {c: f"cowidev.vax.batch.{c}" for c in batch_countries}
country_to_module_incremental = {c: f"cowidev.vax.incremental.{c}" for c in incremental_countries}
country_to_module = {
    **country_to_module_batch,
    **country_to_module_incremental,
}
MODULES_NAME_BATCH = list(country_to_module_batch.values())
MODULES_NAME_INCREMENTAL = list(country_to_module_incremental.values())
MODULES_NAME = MODULES_NAME_BATCH + MODULES_NAME_INCREMENTAL

TIMEOUT = 10


class TimeoutException(Exception):
    pass


class CountryDataGetter:
    def __init__(self, skip_countries: list, gsheets_api):
        self.skip_countries = skip_countries
        self.gsheets_api = gsheets_api

    def run(self, module_name: str):
        t0 = time.time()
        country = module_name.split(".")[-1]
        if country.lower() in self.skip_countries:
            logger.info(f"VAX - {module_name}: skipped! ⚠️")
            return {"module_name": module_name, "success": None, "skipped": True, "time": None}
        args = []
        if country == "colombia":
            args.append(self.gsheets_api)
        logger.info(f"VAX - {module_name}: started")
        module = importlib.import_module(module_name)
        try:
            # with time_limit(TIMEOUT):
            module.main(*args)
        except Exception as err:
            success = False
            logger.error(f"VAX - {module_name}: ❌ {err}", exc_info=True)
        else:
            success = True
            logger.info(f"VAX - {module_name}: SUCCESS ✅")
        t = round(time.time() - t0, 2)
        return {"module_name": module_name, "success": success, "skipped": False, "time": t}


def main_get_data(
    parallel: bool = False,
    n_jobs: int = -2,
    modules_name: list = MODULES_NAME,
    skip_countries: list = [],
    gsheets_api=None,
):
    """Get data from sources and export to output folder.

    Is equivalent to script `run_python_scripts.py`
    """
    t0 = time.time()
    print("-- Getting data... --")
    skip_countries = [x.lower() for x in skip_countries]
    country_data_getter = CountryDataGetter(skip_countries, gsheets_api)
    modules_name = _load_modules_order(modules_name)
    if parallel:
        modules_execution_results = Parallel(n_jobs=n_jobs, backend="threading")(
            delayed(country_data_getter.run)(
                module_name,
            )
            for module_name in modules_name
        )
    else:
        modules_execution_results = []
        for module_name in modules_name:
            modules_execution_results.append(
                country_data_getter.run(
                    module_name,
                )
            )
    t_sec_1 = round(time.time() - t0, 2)
    # Get timing dataframe
    df_exec = _build_df_execution(modules_execution_results)
    # Retry failed modules
    _retry_modules_failes(modules_execution_results, country_data_getter)
    # Print timing details
    _print_timing(t0, t_sec_1, df_exec)


def _build_df_execution(modules_execution_results):
    df_new = (
        pd.DataFrame(
            [
                {"module": m["module_name"], "execution_time (sec)": m["time"], "success": m["success"]}
                for m in modules_execution_results
            ]
        )
        .set_index("module")
        .sort_values(by="execution_time (sec)", ascending=False)
    )
    date_now = localdate(force_today=True)
    df_new = df_new.assign(date=date_now)
    print(len(df_new), len(MODULES_NAME), len(df_new) == len(MODULES_NAME))
    if len(df_new) == len(MODULES_NAME):
        # df = pd.read_csv(os.path.join(paths.SCRIPTS.OUTPUT_VAX_LOG, "get-data.csv"))
        df = df_from_s3("log/vax-get-data.csv")
        df = df[df.date != date_now]
        df = pd.concat([df, df_new.reset_index()])
        df_to_s3(df, "log/vax-get-data.csv")
    return df_new


os.path.join(paths.SCRIPTS.OUTPUT_VAX_LOG, "get-data.csv")


def _load_modules_order(modules_name):
    df = df_from_s3("log/vax-get-data.csv")
    # df = pd.read_csv(os.path.join(paths.SCRIPTS.OUTPUT_VAX_LOG, "get-data.csv"))
    module_order_all = (
        df.sort_values("date")
        .drop_duplicates(subset=["module"], keep="last")
        .sort_values("execution_time (sec)", ascending=False)
        .module.tolist()
    )
    return [m for m in module_order_all if m in modules_name]


def _retry_modules_failes(modules_execution_results, country_data_getter):
    modules_failed = [m["module_name"] for m in modules_execution_results if m["success"] is False]
    logger.info(f"\n---\n\nRETRIES ({len(modules_failed)})")
    modules_execution_results = []
    for module_name in modules_failed:
        modules_execution_results.append(country_data_getter.run(module_name))
    modules_failed_retrial = [m["module_name"] for m in modules_execution_results if m["success"] is False]
    if len(modules_failed_retrial) > 0:
        failed_str = "\n".join([f"* {m}" for m in modules_failed_retrial])
        print(f"\n---\n\nFAILED\nThe following scripts failed to run ({len(modules_failed_retrial)}):\n{failed_str}")


def _print_timing(t0, t_sec_1, df_time):
    t_min_1 = round(t_sec_1 / 60, 2)
    t_sec_2 = round(time.time() - t0, 2)
    t_min_2 = round(t_sec_2 / 60, 2)
    print("---")
    print("TIMING DETAILS")
    print(f"Took {t_sec_1} seconds (i.e. {t_min_1} minutes).")
    print(f"Top 20 most time consuming scripts:")
    print(df_time.head(20))
    print(f"\nTook {t_sec_2} seconds (i.e. {t_min_2} minutes) [AFTER RETRIALS].")
    print_eoe()
