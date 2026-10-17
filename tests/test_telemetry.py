from cargomind import telemetry


def test_usage_costs_and_nested_async_run_scope_are_separate():
    token = telemetry.begin_run()
    try:
        receipt = telemetry.record_model_call("openai/gpt-6.1-sol", {
            "input_tokens": 1000, "output_tokens": 100, "input_tokens_details": {"cached_tokens": 100}}, 50)
        assert abs(receipt["estimated_text_cost_usd"] - .00281) < 1e-12
        summary = telemetry.run_summary()
        assert summary["model_calls"] == 1 and summary["input_tokens"] == 1000
        telemetry.record_model_call("unknown-model", {}, 3, status="failed")
        summary = telemetry.run_summary()
        assert summary["usage_unknown_calls"] == 1 and summary["unpriced_calls"] == 1
        assert summary["failed_calls"] == 1
        assert set(receipt) == {"model", "kind", "status", "input_tokens", "output_tokens", "cached_tokens", "latency_ms", "estimated_text_cost_usd"}
    finally:
        telemetry.end_run(token)
    assert telemetry.run_summary()["model_calls"] == 0
