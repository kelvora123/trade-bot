"""Snapshots, chain diffing, and a full offline pipeline run through the CLI."""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest
import yaml
from conftest import ASOF, EXPIRY, make_chain

from tradebot.cli import main
from tradebot.layer1.snapshots import diff_against_previous, load_snapshot, write_snapshot
from tradebot.store import Store
from tradebot.types import ChainRow, OptionKind

YESTERDAY = ASOF - timedelta(days=1)


# ------------------------------------------------------------------ snapshots


def test_snapshot_writes_dated_json_and_mirrors_to_sqlite(tmp_path, store):
    rows = make_chain()
    path = write_snapshot(tmp_path, store, "NVDA", ASOF, rows, spot=185.0)
    assert path.name == "NVDA_2026-01-05.json"

    payload = json.loads(path.read_text())
    assert payload["ticker"] == "NVDA"
    assert payload["row_count"] == len(rows)
    assert payload["spot"] == 185.0
    assert len(store.chain_contracts("NVDA", ASOF)) == len(rows)


def test_snapshot_roundtrips(tmp_path, store):
    write_snapshot(tmp_path, store, "NVDA", ASOF, make_chain(), 185.0)
    assert load_snapshot(tmp_path, "NVDA", ASOF)["ticker"] == "NVDA"
    assert load_snapshot(tmp_path, "NVDA", date(1999, 1, 1)) is None


def test_snapshot_is_idempotent(tmp_path, store):
    rows = make_chain()
    write_snapshot(tmp_path, store, "NVDA", ASOF, rows, 185.0)
    write_snapshot(tmp_path, store, "NVDA", ASOF, rows, 185.0)
    assert len(store.chain_contracts("NVDA", ASOF)) == len(rows)


# ----------------------------------------------------------------- diffing


def test_first_run_has_no_baseline(tmp_path, store):
    write_snapshot(tmp_path, store, "NVDA", ASOF, make_chain(), 185.0)
    d = diff_against_previous(store, "NVDA", ASOF)
    assert d.baseline is None and not d.has_changes
    assert "no prior snapshot" in d.to_json()["note"].lower()


def test_new_strike_detected_against_prior_run(tmp_path, store):
    write_snapshot(tmp_path, store, "NVDA", YESTERDAY, make_chain(), 184.0)
    today = make_chain() + [
        ChainRow("NVDA", EXPIRY, 210.0, OptionKind.CALL, bid=1.0, ask=1.2, iv=0.55)
    ]
    write_snapshot(tmp_path, store, "NVDA", ASOF, today, 185.0)

    d = diff_against_previous(store, "NVDA", ASOF)
    assert d.baseline == YESTERDAY
    assert len(d.new_strikes) == 1
    assert d.new_strikes[0]["strike"] == 210.0
    assert d.new_expiries == []


def test_new_expiry_detected_and_not_double_counted(tmp_path, store):
    """A whole new expiry is one event, not N new strikes."""
    write_snapshot(tmp_path, store, "NVDA", YESTERDAY, make_chain(), 184.0)
    new_exp = EXPIRY + timedelta(days=28)
    write_snapshot(tmp_path, store, "NVDA", ASOF, make_chain() + make_chain(expiry=new_exp), 185.0)

    d = diff_against_previous(store, "NVDA", ASOF)
    assert d.new_expiries == [new_exp.isoformat()]
    assert d.new_strikes == []  # attributed to the new expiry, not counted twice


def test_removed_expiry_detected(tmp_path, store):
    old = EXPIRY - timedelta(days=28)
    write_snapshot(tmp_path, store, "NVDA", YESTERDAY, make_chain() + make_chain(expiry=old), 184.0)
    write_snapshot(tmp_path, store, "NVDA", ASOF, make_chain(), 185.0)
    assert diff_against_previous(store, "NVDA", ASOF).removed_expiries == [old.isoformat()]


# ------------------------------------------------------------------ e2e


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A complete repo layout with one day of snapshot history already stored."""
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "news": {"enabled": False},
                "macro": {"enabled": False},  # no network in tests
                "paper": {"starting_cash": 100.0, "currency": "GBP"},
            }
        )
    )
    (tmp_path / "config" / "portfolio.yaml").write_text(
        yaml.safe_dump(
            {
                "holdings": [
                    {
                        "ticker": "NVDA",
                        "kind": "option",
                        "option_kind": "call",
                        "qty": 2,
                        "strike": 180.0,
                        "expiry": EXPIRY.isoformat(),
                        "cost_basis": 12.50,
                        "target": 25.0,
                        "stop": 6.0,
                    },
                    {"ticker": "AAPL", "kind": "shares", "qty": 10, "cost_basis": 225.40},
                ]
            }
        )
    )
    (tmp_path / "config" / "sectors.yaml").write_text(
        yaml.safe_dump({"sectors": {"NVDA": "Technology", "AAPL": "Technology"}})
    )

    with Store(tmp_path / "data" / "tradebot.sqlite") as s:
        write_snapshot(tmp_path / "snapshots", s, "NVDA", ASOF, make_chain(), 185.0)
        s.record_spot(ASOF, "AAPL", 230.0)
    return tmp_path


def test_full_offline_run_succeeds(project, capsys):
    code = main(["--root", str(project), "--offline", "--asof", ASOF.isoformat(), "run", "--no-news"])
    assert code == 0
    out = capsys.readouterr().out
    assert "# Portfolio run - 2026-01-05" in out
    assert "Aggregate exposures" in out
    assert "Growth target reality check" in out


def test_full_offline_run_writes_both_reports(project):
    main(["--root", str(project), "--offline", "--asof", ASOF.isoformat(), "run", "--no-news"])
    reports = project / "reports"
    assert (reports / "report_2026-01-05.md").exists()
    assert (reports / "report_2026-01-05.json").exists()
    assert (reports / "latest.md").exists()
    assert (reports / "latest.json").exists()


def test_run_json_payload_has_every_layer(project, capsys):
    main(["--root", str(project), "--offline", "--asof", ASOF.isoformat(), "run", "--no-news", "--json"])
    payload = json.loads(capsys.readouterr().out)
    for key in ("layer1_valuation", "layer2_analytics", "layer3_news", "paper_account", "growth_target"):
        assert key in payload
    assert payload["layer1_valuation"]["totals"]["current_value"] == pytest.approx(4900.0)
    assert payload["layer3_news"]["usage"]["api_calls"] == 0


def test_run_persists_layers_for_later_diffing(project):
    main(["--root", str(project), "--offline", "--asof", ASOF.isoformat(), "run", "--no-news"])
    with Store(project / "data" / "tradebot.sqlite") as s:
        assert s.load_run(ASOF, "layer1") is not None
        assert s.load_run(ASOF, "layer2") is not None


def test_value_subcommand(project, capsys):
    assert main(["--root", str(project), "--offline", "--asof", ASOF.isoformat(), "value"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["layer"] == "1_data_valuation"
    assert len(payload["positions"]) == 2


def test_paper_affordability_check_rejects_on_small_account(project, capsys):
    code = main(
        [
            "--root", str(project), "paper",
            "--check-affordable", "NVDA260320C00180000",
            "--qty", "1", "--mark", "13.0",
        ]
    )
    assert code == 1  # GBP100 cannot buy a $1,300 contract
    assert "Not affordable" in capsys.readouterr().err


def test_usage_subcommand_reports_zero_spend(project, capsys):
    assert main(["--root", str(project), "usage"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["total_input_tokens"] == 0
    assert payload["estimated_total_cost_usd"] == 0.0


def test_missing_portfolio_fails_cleanly(tmp_path, capsys):
    (tmp_path / "config").mkdir()
    assert main(["--root", str(tmp_path), "--offline", "run"]) == 1
