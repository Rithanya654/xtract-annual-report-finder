import os
from datetime import datetime, timezone


OPENAI_PRICE_USD_PER_MTOK = {
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (5.00, 15.00),
    "gpt-5.4-nano": (0.20, 1.25),
    "gpt-5.4-mini": (0.75, 4.50),
    "gpt-5.4": (2.50, 15.00),
    "gpt-5.5": (5.00, 30.00),
}


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _model_prices(model: str, input_env: str, output_env: str) -> tuple[float, float]:
    default_input, default_output = OPENAI_PRICE_USD_PER_MTOK.get(model, (0.0, 0.0))
    return _env_float(input_env, default_input), _env_float(output_env, default_output)


def _as_utc(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    return None


def estimate_run_cost(run: dict, counts: dict) -> dict:
    """Estimate cost from duration, row counts, model settings, and env-configured rates."""
    now = datetime.now(timezone.utc)
    started_at = _as_utc(run.get("created_at")) or now
    finished_at = _as_utc(run.get("finished_at")) or now
    duration_hours = max(0.0, (finished_at - started_at).total_seconds() / 3600)

    total_rows = counts.get("total", 0) or 0
    found_rows = counts.get("found", 0) or 0
    not_found_rows = counts.get("not_found", 0) or 0

    worker_count = _env_int("XTRACT_COST_WORKER_COUNT", _env_int("SPOT_WORKER_COUNT", 0))
    worker_hourly = _env_float("XTRACT_COST_WORKER_HOURLY_USD", 0.0)
    server_hourly = _env_float("XTRACT_COST_SERVER_HOURLY_USD", 0.0)
    compute_usd = duration_hours * ((worker_count * worker_hourly) + server_hourly)

    landing_model = os.environ.get("OPENAI_LANDING_MODEL", "gpt-4o-mini").strip()
    verify_model = os.environ.get("OPENAI_VERIFY_MODEL", "gpt-4o-mini").strip()
    landing_in_price, landing_out_price = _model_prices(
        landing_model,
        "XTRACT_COST_OPENAI_LANDING_INPUT_USD_PER_MTOK",
        "XTRACT_COST_OPENAI_LANDING_OUTPUT_USD_PER_MTOK",
    )
    verify_in_price, verify_out_price = _model_prices(
        verify_model,
        "XTRACT_COST_OPENAI_VERIFY_INPUT_USD_PER_MTOK",
        "XTRACT_COST_OPENAI_VERIFY_OUTPUT_USD_PER_MTOK",
    )

    landing_calls = total_rows * _env_float("XTRACT_COST_LANDING_CALLS_PER_ROW", 1.0)
    verify_calls = total_rows * _env_float("XTRACT_COST_VERIFY_CALLS_PER_ROW", 2.0)
    landing_input_tokens = landing_calls * _env_float("XTRACT_COST_LANDING_INPUT_TOKENS", 600)
    landing_output_tokens = landing_calls * _env_float("XTRACT_COST_LANDING_OUTPUT_TOKENS", 120)
    verify_input_tokens = verify_calls * _env_float("XTRACT_COST_VERIFY_INPUT_TOKENS", 2500)
    verify_output_tokens = verify_calls * _env_float("XTRACT_COST_VERIFY_OUTPUT_TOKENS", 80)

    openai_usd = (
        (landing_input_tokens / 1_000_000 * landing_in_price)
        + (landing_output_tokens / 1_000_000 * landing_out_price)
        + (verify_input_tokens / 1_000_000 * verify_in_price)
        + (verify_output_tokens / 1_000_000 * verify_out_price)
    )

    gemini_enabled = os.environ.get("GEMINI_SEARCH_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
    gemini_calls = 0.0
    gemini_usd = 0.0
    if gemini_enabled:
        gemini_calls = not_found_rows * _env_float("XTRACT_COST_GEMINI_CALLS_PER_NOT_FOUND", 1.0)
        gemini_usd = gemini_calls * _env_float("XTRACT_COST_GEMINI_SEARCH_USD_PER_CALL", 0.035)

    s3_usd = found_rows * _env_float("XTRACT_COST_S3_PER_FOUND_PDF_USD", 0.0)
    other_usd = (
        _env_float("XTRACT_COST_OTHER_PER_RUN_USD", 0.0)
        + _env_float("XTRACT_COST_EMAIL_PER_RUN_USD", 0.0)
        + s3_usd
    )

    api_usd = openai_usd + gemini_usd
    total_usd = compute_usd + api_usd + other_usd

    return {
        "currency": os.environ.get("XTRACT_COST_CURRENCY", "USD"),
        "total_usd": round(total_usd, 4),
        "compute_usd": round(compute_usd, 4),
        "api_usd": round(api_usd, 4),
        "openai_usd": round(openai_usd, 4),
        "gemini_usd": round(gemini_usd, 4),
        "other_usd": round(other_usd, 4),
        "duration_hours": round(duration_hours, 3),
        "worker_count": worker_count,
        "worker_hourly_usd": worker_hourly,
        "server_hourly_usd": server_hourly,
        "landing_model": landing_model,
        "verify_model": verify_model,
        "landing_calls_est": round(landing_calls, 2),
        "verify_calls_est": round(verify_calls, 2),
        "gemini_calls_est": round(gemini_calls, 2),
        "estimated": True,
    }
