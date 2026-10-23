from concurrent.futures import ThreadPoolExecutor
from decimal import Inexact, ROUND_DOWN, localcontext

import pytest

from capacity_estimation import estimate, parse_bytes, sweep


def test_model_is_independent_of_callers_decimal_context():
    expected = estimate({})
    with localcontext() as context:
        context.prec = 3
        context.rounding = ROUND_DOWN
        context.Emax = 3
        context.traps[Inexact] = True
        assert estimate({}) == expected
        assert parse_bytes("1 PB") == 10**15
        assert context.prec == 3
        assert context.traps[Inexact]


def test_concurrent_callers_have_isolated_numeric_settings():
    expected = estimate({})

    def calculate(precision):
        with localcontext() as context:
            context.prec = precision
            context.rounding = ROUND_DOWN
            context.traps[Inexact] = True
            result = estimate({})
            assert context.prec == precision
            return result

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert all(value == expected for value in pool.map(calculate, range(2, 18)))


@pytest.mark.parametrize("inputs,values", [(None, [1]), ({}, None), ({}, "12")])
def test_invalid_sweep_containers_have_consistent_validation(inputs, values):
    with pytest.raises(ValueError):
        sweep(inputs, "peak_factor", values)
