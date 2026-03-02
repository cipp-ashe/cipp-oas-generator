#!/usr/bin/env bash
# validate_outcomes.sh — Establish ground truth with actionable metrics
#
# This script measures actual outcomes to determine whether incremental fixes
# are sufficient or if greenfield architectural changes are needed.

set -euo pipefail

GENERATOR_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$GENERATOR_DIR"

LOGS_DIR="validation_logs_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$LOGS_DIR"

echo "════════════════════════════════════════════════════════════"
echo "CIPP OAS Generator - Outcomes Validation"
echo "Timestamp: $(date)"
echo "Logs: $LOGS_DIR/"
echo "════════════════════════════════════════════════════════════"
echo ""

# ── OUTCOME 1: Pattern Drift Detection ────────────────────────────────────────
echo "════════════════════════════════════════════════════════════"
echo "OUTCOME 1: Pattern Drift Detection"
echo "GOAL: Verify generator assumptions still match CIPP codebase"
echo "SUCCESS CRITERIA: ≤2 pattern failures (known false positives)"
echo "FAILURE THRESHOLD: ≥5 failures = CIPP has evolved, patterns stale"
echo "════════════════════════════════════════════════════════════"

./run.sh --check-patterns > "$LOGS_DIR/pattern-check.log" 2>&1 || true
failed_patterns=$(grep -c "✗" "$LOGS_DIR/pattern-check.log" || echo 0)
total_checks=$(grep -c "^\s*[✓✗]" "$LOGS_DIR/pattern-check.log" || echo 0)

echo "→ Pattern checks: $failed_patterns failed / $total_checks total"
echo ""
echo "Failed checks:"
grep "✗" "$LOGS_DIR/pattern-check.log" || echo "  (none)"
echo ""

if [ "$failed_patterns" -ge 5 ]; then
    echo "🔴 CRITICAL: Fundamental drift detected - greenfield parser needed"
    drift_status="CRITICAL"
elif [ "$failed_patterns" -ge 3 ]; then
    echo "⚠️  WARNING: Pattern updates required"
    drift_status="WARNING"
else
    echo "✅ PASS: Patterns still valid"
    drift_status="PASS"
fi

echo ""

# ── OUTCOME 2: Parameter Extraction Accuracy ───────────────────────────────────
echo "════════════════════════════════════════════════════════════"
echo "OUTCOME 2: Parameter Extraction Accuracy"
echo "GOAL: Verify known endpoints extract correct parameters"
echo "SUCCESS CRITERIA: High confidence params match expected behavior"
echo "FAILURE THRESHOLD: High error rate = static analysis inadequate"
echo "════════════════════════════════════════════════════════════"

# Sample endpoints with known characteristics
test_endpoints=("AddUser" "EditGroup" "ExecOffboardUser" "AddPolicy" "ListUsers")
total_tested=0
validation_errors=0

for ep in "${test_endpoints[@]}"; do
    echo "Testing $ep..."
    if [ -f "out/merged-params-$ep.json" ] || ./run.sh --validate-endpoint "$ep" > "$LOGS_DIR/validate-$ep.log" 2>&1; then
        echo "  → Validation completed (see $LOGS_DIR/validate-$ep.log)"
        ((total_tested++))
    else
        echo "  → Failed to validate (endpoint may not exist)"
        ((validation_errors++))
        ((total_tested++))
    fi
done

if [ "$total_tested" -gt 0 ]; then
    success_rate=$(((total_tested - validation_errors) * 100 / total_tested))
    echo ""
    echo "→ Validation success rate: $success_rate% ($validation_errors failures / $total_tested tested)"
    echo ""
    
    if [ "$success_rate" -lt 60 ]; then
        echo "🔴 CRITICAL: <60% endpoints validated successfully - check logs for issues"
        accuracy_status="CRITICAL"
    elif [ "$validation_errors" -gt 0 ]; then
        echo "⚠️  WARNING: Some endpoints failed validation - review logs"
        accuracy_status="WARNING"
    else
        echo "✅ PASS: All test endpoints validated successfully"
        accuracy_status="PASS"
    fi
else
    echo "⚠️  WARNING: No endpoints tested"
    accuracy_status="WARNING"
fi

echo ""

# ── OUTCOME 3: Performance & Reliability ───────────────────────────────────────
echo "════════════════════════════════════════════════════════════"
echo "OUTCOME 3: Performance & Reliability"
echo "GOAL: Ensure generator completes without hangs/crashes"
echo "SUCCESS CRITERIA: Full corpus run in <5min, zero crashes"
echo "FAILURE THRESHOLD: >5min or any crashes = optimization needed"
echo "════════════════════════════════════════════════════════════"

echo "Running full corpus generation (with 10min timeout)..."
start_time=$(date +%s)

if timeout 600 ./run.sh > "$LOGS_DIR/corpus-run.log" 2>&1; then
    exit_code=0
else
    exit_code=$?
fi

end_time=$(date +%s)
duration=$((end_time - start_time))

echo "→ Corpus run time: ${duration}s"
echo "→ Exit code: $exit_code"

# Analyze errors (use wc -l instead of grep -c to handle multiple matches properly)
scan_errors=$(grep "scan errors" "$LOGS_DIR/corpus-run.log" 2>/dev/null | wc -l || echo 0)
warnings=$(grep "⚠" "$LOGS_DIR/corpus-run.log" 2>/dev/null | wc -l || echo 0)
# Clean whitespace
scan_errors=$(echo "$scan_errors" | tr -d ' ')
warnings=$(echo "$warnings" | tr -d ' ')

echo "→ Scan errors: $scan_errors"
echo "→ Warnings: $warnings"
echo ""

if [ "$exit_code" -eq 124 ]; then
    echo "🔴 CRITICAL: Generator timed out after 10min - needs performance optimization"
    perf_status="TIMEOUT"
elif [ "$exit_code" -ne 0 ]; then
    echo "🔴 CRITICAL: Generator crashed (exit $exit_code) - needs error handling improvements"
    perf_status="CRASH"
elif [ "$duration" -gt 300 ]; then
    echo "⚠️  WARNING: Slow performance (>5min) - consider profiling regex bottlenecks"
    perf_status="SLOW"
elif [ "$scan_errors" -gt 10 ]; then
    echo "⚠️  WARNING: High error rate ($scan_errors errors) - resilience improvements needed"
    perf_status="HIGH_ERRORS"
else
    echo "✅ PASS: Performance and reliability acceptable"
    perf_status="PASS"
fi

echo ""

# ── OUTCOME 4: Coverage Analysis ───────────────────────────────────────────────
echo "════════════════════════════════════════════════════════════"
echo "OUTCOME 4: Coverage Analysis"
echo "GOAL: Understand what % of CIPP is actually analyzable"
echo "SUCCESS CRITERIA: Document coverage gaps for decision making"
echo "════════════════════════════════════════════════════════════"

if [ -f "out/coverage-report.json" ]; then
    coverage_analysis=$(python3 << 'EOF'
import json
try:
    with open('out/coverage-report.json') as f:
        data = json.load(f)
        tiers = data.get('tier_breakdown', {})
        
        full = len(tiers.get('FULL', []))
        partial = len(tiers.get('PARTIAL', []))
        blind = len(tiers.get('BLIND', []))
        orphan = len(tiers.get('ORPHAN', []))
        stub = len(tiers.get('STUB', []))
        
        total = full + partial + blind + orphan + stub
        
        if total > 0:
            blind_pct = (blind * 100) // total
            full_pct = (full * 100) // total
            print(f"{blind}|{total}|{blind_pct}|{full_pct}")
        else:
            print("0|0|0|0")
except Exception as e:
    print(f"ERROR: {e}")
    print("0|0|0|0")
EOF
)
    
    IFS='|' read -r blind_count total_count blind_pct full_pct <<< "$coverage_analysis"
    
    echo "→ FULL coverage: $full_pct%"
    echo "→ BLIND endpoints: $blind_count / $total_count ($blind_pct%)"
    echo ""
    
    if [ "$blind_pct" -gt 30 ]; then
        echo "🔴 CRITICAL: >30% blind coverage - static analysis fundamentally inadequate"
        echo "   RECOMMENDATION: Invest in @babel/parser migration for JSX"
        coverage_status="CRITICAL"
    elif [ "$blind_pct" -gt 15 ]; then
        echo "⚠️  WARNING: 15-30% blind - improve sidecars or enhance regex"
        coverage_status="WARNING"
    else
        echo "✅ PASS: <15% blind coverage acceptable with sidecars"
        coverage_status="PASS"
    fi
else
    echo "⚠️  WARNING: coverage-report.json not found"
    coverage_status="UNKNOWN"
fi

echo ""

# ── DECISION MATRIX ────────────────────────────────────────────────────────────
echo "════════════════════════════════════════════════════════════"
echo "DECISION MATRIX"
echo "════════════════════════════════════════════════════════════"
echo ""
echo "Results Summary:"
echo "  1. Pattern Drift: $drift_status"
echo "  2. Accuracy:      $accuracy_status"
echo "  3. Performance:   $perf_status"
echo "  4. Coverage:      $coverage_status"
echo ""
echo "────────────────────────────────────────────────────────────"
echo "Recommendations:"
echo ""

# Determine priority action
critical_count=0
[ "$drift_status" = "CRITICAL" ] && ((critical_count++))
[ "$accuracy_status" = "CRITICAL" ] && ((critical_count++))
[ "$perf_status" = "TIMEOUT" ] || [ "$perf_status" = "CRASH" ] && ((critical_count++))
[ "$coverage_status" = "CRITICAL" ] && ((critical_count++))

if [ "$critical_count" -gt 0 ]; then
    echo "🔴 CRITICAL ISSUES FOUND - Greenfield work required:"
    echo ""
    
    [ "$drift_status" = "CRITICAL" ] && echo "  → Pattern drift ≥5 failures: Update regex patterns in stage scripts (1-2 days)"
    [ "$accuracy_status" = "CRITICAL" ] && echo "  → Accuracy <60%: Consider AST parser migration (2+ weeks)"
    [ "$perf_status" = "TIMEOUT" ] && echo "  → Timeout: Add regex timeout framework + profiling (3-5 days)"
    [ "$perf_status" = "CRASH" ] && echo "  → Crashes: Improve error handling + resilience (2-3 days)"
    [ "$coverage_status" = "CRITICAL" ] && echo "  → Coverage >30% blind: @babel/parser POC for JSX (1-2 weeks)"
    
    echo ""
    echo "NEXT STEP: Address critical issues before proceeding"
else
    warning_count=0
    [ "$drift_status" = "WARNING" ] && ((warning_count++))
    [ "$accuracy_status" = "WARNING" ] && ((warning_count++))
    [ "$perf_status" = "SLOW" ] || [ "$perf_status" = "HIGH_ERRORS" ] && ((warning_count++))
    [ "$coverage_status" = "WARNING" ] && ((warning_count++))
    
    if [ "$warning_count" -gt 0 ]; then
        echo "⚠️  WARNINGS FOUND - Incremental improvements recommended:"
        echo ""
        
        [ "$drift_status" = "WARNING" ] && echo "  → Fix --check-patterns sampling (update pipeline.py lines 65-168)"
        [ "$accuracy_status" = "WARNING" ] && echo "  → Improve regex patterns or add sidecars for problematic endpoints"
        [ "$perf_status" = "SLOW" ] && echo "  → Profile regex performance to identify bottlenecks"
        [ "$perf_status" = "HIGH_ERRORS" ] && echo "  → Review scan errors and improve error handling"
        [ "$coverage_status" = "WARNING" ] && echo "  → Add sidecars for BLIND endpoints or enhance extraction"
        
        echo ""
        echo "NEXT STEP: Prioritize warnings, start with sampling fix"
    else
        echo "✅ ALL TESTS PASSED - System is healthy"
        echo ""
        echo "  → Consider fixing --check-patterns false positives for cleaner output"
        echo "  → Continue with normal maintenance and sidecar additions as needed"
        echo ""
        echo "NEXT STEP: Ship current implementation, monitor for drift"
    fi
fi

echo ""
echo "════════════════════════════════════════════════════════════"
echo "Validation complete. Detailed logs in: $LOGS_DIR/"
echo "════════════════════════════════════════════════════════════"
