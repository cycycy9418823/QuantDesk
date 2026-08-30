from __future__ import annotations

import numpy as np
import pandas as pd

from quantdesk.risk.var import historical_var, portfolio_var


def test_historical_var_outputs_tail_metrics():
    returns = pd.Series(np.linspace(-0.05, 0.05, 200))
    result = historical_var(returns)
    assert result["observations"] == 200
    assert result["var_95"] < 0
    assert result["cvar_95"] <= result["var_95"]


def test_portfolio_var_handles_weights():
    df = pd.DataFrame({"a": np.linspace(-0.02, 0.03, 200), "b": np.linspace(-0.01, 0.02, 200)})
    result = portfolio_var({"a": 0.6, "b": 0.4}, df)
    assert result["observations"] == 200
    assert "var_95" in result
