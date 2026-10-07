"""Publish compact verified research results and a reproducible diagnostic figure."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def build(study, destination, figure):
    from analyze import clean, stats, write
    plan = read(HERE / "plan.json")
    stages = [read(study / f"{stage}-summary.json") for stage in ("develop", "audit")]
    assert stages[0]["decision"] == stages[1]["decision"], "Reused audit changed selection"
    for result in stages:
        assert result["provenance"]["plan_sha256"] == sha(HERE / "plan.json")
    summaries = [row for result in stages for row in result["summaries"]]
    paths = [row for result in stages for row in result["path_diagnostics"]]
    records = pd.concat([pd.read_json(study / f"{stage}-trades.jsonl.gz", lines=True)
                         for stage in ("develop", "audit")], ignore_index=True)
    closed = records.loc[records.status.eq("closed")]
    rates = plan["costs"]
    independent_net = (closed.exit_raw*(1-closed.slippage)*(1-rates["sell_fee"]-rates["sell_tax"]) /
                       (closed.entry_raw*(1+closed.slippage)*(1+rates["buy_fee"])) - 1)
    error = float((independent_net-closed.net_return).abs().max())
    assert error < 1e-8, "Cashflow audit mismatch"
    qualified = records.loc[records.paired_observed & records.slippage.eq(.001)]
    exits = []
    for (period, rule), group in qualified.groupby(["period", "rule_id"], sort=False):
        max_hold = next(r["max_hold"] for r in plan["rules"] if r["id"] == rule)
        early = group.loc[group.holding_days.lt(max_hold)]
        exits.append(dict(period=period, rule=rule, paired_trades=len(group), early_exits=len(early),
            early_exit_later_profitable_rate=float(early.baseline_net.gt(0).mean()) if len(early) else None,
            early_exit_worse_than_hold_rate=float(early.net_return.lt(early.baseline_net).mean()) if len(early) else None,
            paired_trade_delta_mean=float((group.net_return-group.baseline_net).mean()),
            net_q05=float(group.net_return.quantile(.05)), hold_q05=float(group.baseline_net.quantile(.05)),
            note="동일 진입·종목 완결 짝표본의 거래별 통계. 사후 경로 진단이며 청산시 알 수 없는 값."))
    costs = []
    noise = []
    for (period, rule), group in qualified.groupby(["period", "rule_id"], sort=False):
        daily = group.groupby("signal_date")[["raw_return", "net_return"]].mean()
        costs.append(dict(period=period, rule=rule, raw_day_mean=float(daily.raw_return.mean()),
                          net_day_mean=float(daily.net_return.mean()),
                          cost_drag=float((daily.raw_return-daily.net_return).mean())))
        if rule == "time5":
            winners = group.loc[group.net_return.gt(0)]
            losers = group.loc[group.net_return.le(0)]
            noise.append(dict(period=period, winners=len(winners), losers=len(losers),
                winner_close_mae_median=float(winners.close_mae.median()),
                loser_close_mae_median=float(losers.close_mae.median()),
                winners_with_3pct_close_drawdown=float(winners.close_mae.le(-.03).mean()),
                note="사후 이익거래도 겪은 흔들림 진단. 진입시 승패를 구분할 수 있다는 뜻 아님."))
    context_path = study / "context-diagnostics.json"
    context = read(context_path) if context_path.exists() else None
    independent_path = ROOT / ".local/research/daily-trend-independent-2026-10-04.json"
    independent = read(independent_path)
    assert independent["engine_sha256"] == sha(HERE / "engine.py")
    baseline_path = study / "baseline-verification.json"
    baseline = read(baseline_path)
    assert baseline["summary"]["differences"] == 0
    assert baseline["verifier_sha256"] == sha(HERE / "verify_baseline.py")
    for name, expected in baseline["reference_hashes"].items():
        assert sha(study / name) == expected, "Baseline audit is stale"
    result = dict(status=stages[0]["decision"]["status"], decision=stages[0]["decision"],
                  periods=plan["periods"], plan_sha256=sha(HERE / "plan.json"),
                  study_provenance=[r["provenance"] for r in stages],
                  summaries=summaries, paths=paths, exit_diagnostics=exits, cost_diagnostics=costs, noise_diagnostics=noise,
                  lookback_sensitivity=stages[0]["lookback_sensitivity"],
                  conditional_diagnostics=[r for stage in stages for r in stage["conditional_diagnostics"]],
                  context_artifact=str(context_path.relative_to(ROOT)),
                  context_sha256=sha(context_path) if context else None,
                  independent_verification=dict(summary=independent["summary"], package=independent["package"],
                                                path=str(independent_path.relative_to(ROOT)), sha256=sha(independent_path)),
                  baseline_verification=dict(summary=baseline["summary"], path=str(baseline_path.relative_to(ROOT)), sha256=sha(baseline_path)),
                  report_code_sha256=sha(__file__),
                  cashflow_audit=dict(closed_records=len(closed), maximum_absolute_difference=error),
                  source_files={str((study / f"{stage}-{kind}").relative_to(ROOT)):sha(study / f"{stage}-{kind}")
                                for stage in ("develop", "audit") for kind in ("summary.json", "trades.jsonl.gz", "daily.csv")},
                  claim="배당 제외 수정가격·재사용 고정종목군 신호 진단. 계좌성과/실제체결/미사용검증 아님.")
    write(destination, result)
    sys.path.insert(0, str(ROOT / ".local/research/daily-trend-viz-env"))
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.font_manager import FontProperties
    font = FontProperties(fname="C:/Windows/Fonts/malgun.ttf")
    plt.rcParams.update({"font.family":font.get_name(), "axes.unicode_minus":False, "font.size":11,
                         "axes.spines.top":False, "axes.spines.right":False, "axes.titlepad":12})
    fig, axes = plt.subplots(1, 2, figsize=(14, 6.4), gridspec_kw={"width_ratios":[1, 1.4]})
    labels = {"development":"개발 2025.02~12", "validation":"검증 2026.01~06", "reused_audit":"재사용 2026.07~10.02"}
    colors = {"development":"#2563a0", "validation":"#a76812", "reused_audit":"#52525b"}
    markers = {"development":"o", "validation":"s", "reused_audit":"^"}
    for period in labels:
        rows = [r for r in paths if r["period"]==period and r["market"]=="ALL"]
        x = [int(r["rule"].removeprefix("time")) for r in rows]
        y = [r["net_day"]["mean"]*100 for r in rows]
        axes[0].plot(x,y,marker=markers[period],color=colors[period],label=labels[period],linewidth=1.7)
    axes[0].axhline(0,color="#999999",linewidth=.8)
    axes[0].set(xticks=[1,3,5,10,20],xlabel="보유 거래일 후 다음 시가 청산",ylabel="신호일 평균 비용 후 수익 (%)",title="돌파 뒤 상승 지속성")
    axes[0].legend(frameon=False,fontsize=10)
    names = [r["id"] for r in plan["rules"]]
    pretty = ["5일 보유", "10일 보유", "초안 3%·10일선", "돌파 실패·5일", "돌파 실패·10일", "2ATR 추적·5일", "2ATR 추적·10일"]
    for i, period in enumerate(labels):
        for j, name in enumerate(names):
            row = next(r for r in summaries if r["period"]==period and r["rule"]==name and r["market"]=="ALL" and r["slip"]==.001)
            y, x = j+(i-1)*.21, row["edge_day"]["mean"]*100
            interval = row["edge_ci95"]
            if interval:
                axes[1].plot([interval[0]*100, interval[1]*100], [y,y],color=colors[period],linewidth=1.3)
            axes[1].plot(x,y,marker=markers[period],color=colors[period],markersize=5)
    axes[1].axvline(0,color="#999999",linewidth=.8)
    axes[1].set(yticks=list(range(len(names))),yticklabels=pretty,xlabel="동일 날짜 유동성 대조군 대비 차이 (%p)",title="청산을 바꾸면 우위가 생기는가")
    axes[1].invert_yaxis()
    for ax in axes:
        ax.grid(axis="x",color="#e6e6e6",linewidth=.7)
        ax.set_axisbelow(True)
    fig.suptitle("수일 돌파매매 진단 — 7개 비교안 모두 미채택",x=.06,ha="left",fontsize=17,fontweight="bold")
    fig.text(.06,.015,"고정 351종목에서 일별 관찰 목록 재구성 · 편도 슬리피지 0.1% + 수수료·세금\n"
             "선: 신호일 20일 블록 95% 구간(선택 판단은 7비교 보정). 오른쪽 재사용 17~18일은 구간 미산출.\n"
             "왼쪽은 만기에 따라 완결 표본 수가 다름. 모든 기간은 탐색 자료이며 계좌 누적수익이 아님.",fontsize=9,color="#555555")
    fig.tight_layout(rect=[.02,.12,.99,.91],w_pad=3)
    Path(figure).parent.mkdir(parents=True,exist_ok=True)
    fig.savefig(figure,dpi=170,facecolor="white")
    plt.close(fig)
    print(json.dumps(dict(status=result["status"], summaries=len(summaries), cashflow_audit=result["cashflow_audit"],
                          destination=str(destination),figure=str(figure)),ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--study", type=Path, default=ROOT / ".local/research/daily-trend-2026-10-04-v2")
    parser.add_argument("--output", type=Path, default=HERE / "results.json")
    parser.add_argument("--figure", type=Path, default=ROOT / "docs/images/daily-trend-2026-10-05.png")
    args = parser.parse_args()
    build(args.study,args.output,args.figure)
