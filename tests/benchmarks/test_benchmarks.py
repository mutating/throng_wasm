"""Measure the same prepared scenarios exposed by the installed library."""

import pytest

from throng_wasm import benchmarks


@pytest.fixture(scope='module')
def suite():
    with benchmarks.prepare() as prepared:
        yield prepared


@pytest.mark.benchmark
@pytest.mark.parametrize('name', benchmarks.SCENARIO_NAMES)
def test_benchmark_scenario(benchmark, suite, name):
    scenario = suite.scenarios[name]
    if name != 'startup.wasm_new_runtime_memory':
        scenario._call_once()
    benchmark(scenario._call_once)
