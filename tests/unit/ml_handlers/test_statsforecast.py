import time
from unittest.mock import patch
import numpy as np
import pandas as pd

from statsforecast.models import AutoCES
from statsforecast.utils import AirPassengersDF
from statsforecast import StatsForecast
from mindsdb.integrations.utilities.time_series_utils import get_best_model_from_results_df
from mindsdb.integrations.handlers.statsforecast_handler.statsforecast_handler import choose_model, model_dict
from mindsdb_sql import parse_sql
from tests.unit.ml_handlers.test_time_series_utils import create_mock_df
from tests.unit.executor_test_base import BaseExecutorTest


def test_choose_model():
    # With this data and settings, AutoETS should win
    model_args = {"horizon": 1, "frequency": "M", "model_name": "auto"}
    sample_df = AirPassengersDF.iloc[:10]
    best_model = choose_model(model_args, sample_df)
    assert best_model.alias == "AutoETS"


class TestStatsForecast(BaseExecutorTest):
    def wait_predictor(self, project, name):
        # wait
        done = False
        for attempt in range(200):
            ret = self.run_sql(f"select * from {project}.models where name='{name}'")
            if not ret.empty:
                if ret["STATUS"][0] == "complete":
                    done = True
                    break
                elif ret["STATUS"][0] == "error":
                    break
            time.sleep(0.5)
        if not done:
            raise RuntimeError("predictor wasn't created")

    def run_sql(self, sql):
        ret = self.command_executor.execute_command(parse_sql(sql, dialect="mindsdb"))
        assert ret.error_code is None
        if ret.data is not None:
            columns = [col.alias if col.alias is not None else col.name for col in ret.columns]
            return pd.DataFrame(ret.data, columns=columns)

    @patch("mindsdb.integrations.handlers.postgres_handler.Handler")
    def test_grouped(self, mock_handler):
        # create project
        self.run_sql("create database proj")
        df = create_mock_df()
        self.set_handler(mock_handler, name="pg", tables={"df": df})

        self.run_sql(
            """
           create model proj.model_1_group
           from pg (select * from df)
           predict target_col
           order by time_col
           group by group_col
           horizon 3
           using
             engine='statsforecast'
        """
        )
        self.wait_predictor("proj", "model_1_group")

        # run predict
        result_df = self.run_sql(
            """
           SELECT p.*
           FROM pg.df as t
           JOIN proj.model_1_group as p
           where t.group_col='b'
        """
        )
        assert list(round(result_df["target_col"])) == [42, 43, 44]

        # now add more groups
        self.run_sql(
            """
           create model proj.model_multi_group
           from pg (select * from df)
           predict target_col
           order by time_col
           group by group_col, group_col_2, group_col_3
           horizon 3
           using
             engine='statsforecast'
        """
        )
        self.wait_predictor("proj", "model_multi_group")

        result_df = self.run_sql(
            """
           SELECT p.*
           FROM pg.df as t
           JOIN proj.model_multi_group as p
           where t.group_col_2='a2'
        """
        )
        assert list(round(result_df["target_col"])) == [32, 33, 34]

    @patch("mindsdb.integrations.handlers.postgres_handler.Handler")
    def test_model_choice(self, mock_handler):
        """This tests whether changing the model_name and frequency USING args
        will switch the actual model used.
        """

        # create project
        self.run_sql("create database proj")
        df = pd.read_parquet("https://datasets-nixtla.s3.amazonaws.com/m4-hourly.parquet")
        df = df[df.unique_id.isin(["H1", "H2", "H3"])]  # subset for speed
        n_groups = df["unique_id"].nunique()
        self.set_handler(mock_handler, name="pg", tables={"df": df})

        # generate ground truth predictions from the package
        prediction_horizon = 4
        sf = StatsForecast(models=[AutoCES(season_length=24)], freq="H")
        sf.fit(df)
        forecast_df = sf.predict(prediction_horizon)
        package_predictions = forecast_df.reset_index(drop=True).iloc[:, -1]

        # create predictor
        self.run_sql(
            f"""
           create model proj.modelx
           from pg (select * from df)
           predict y
           order by ds
           group by unique_id
           horizon {prediction_horizon}
           using
             engine='statsforecast',
             model_name='AutoCES',
             frequency='H'
        """
        )
        self.wait_predictor("proj", "modelx")

        # run predict
        result_df = self.run_sql(
            """
           SELECT p.*
           FROM pg.df as t
           JOIN proj.modelx as p
        """
        )

        # check against ground truth
        mindsdb_result = result_df.iloc[:, -1]
        assert len(mindsdb_result) == prediction_horizon * n_groups
        assert np.allclose(mindsdb_result, package_predictions)

    @patch("mindsdb.integrations.handlers.postgres_handler.Handler")
    def test_auto_model_selection(self, mock_handler):
        """Tests the argument for auto model selection will pick the
        model with the lowest error.
        """
        # create project
        self.run_sql("create database proj")
        self.set_handler(mock_handler, name="pg", tables={"df": AirPassengersDF})

        # generate ground truth predictions from the package - AutoTheta should win here
        prediction_horizon = 1
        sf = StatsForecast(models=[m(season_length=12) for m in model_dict.values()], freq="M", df=AirPassengersDF)
        sf.cross_validation(prediction_horizon, fitted=True)
        sf_results_df = sf.cross_validation_fitted_values()
        best_model_name = get_best_model_from_results_df(sf_results_df)
        package_predictions = sf.forecast(prediction_horizon)[best_model_name]

        # create predictor
        self.run_sql(
            f"""
           create model proj.modelx
           from pg (select * from df)
           predict y
           order by ds
           group by unique_id
           horizon {prediction_horizon}
           using
             engine='statsforecast',
             model_name='auto',
             frequency='M'
        """
        )
        self.wait_predictor("proj", "modelx")

        # run predict
        result_df = self.run_sql(
            """
           SELECT p.*
           FROM pg.df as t
           JOIN proj.modelx as p
        """
        )

        # check against ground truth
        mindsdb_result = result_df.iloc[:, -1]
        assert np.allclose(mindsdb_result, package_predictions)
