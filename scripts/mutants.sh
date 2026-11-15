#!/usr/bin/env bash
# Mutation gate: mutate the read/classify/match/write path (scope in [tool.mutmut], pyproject.toml),
# run the unit tests against every mutant, and fail on any survivor not listed in the baseline.
#
#   scripts/mutants.sh            run the gate
#   scripts/mutants.sh --update   run, then rewrite the baseline to the current survivor set,
#                                 keeping existing reasons and marking new entries UNCLASSIFIED
#
# The spinner output has multibyte characters, so tr/sort/comm run under LC_ALL=C (byte semantics).
# Always starts from a clean mutants/ tree so a stale cache cannot hide a survivor.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

readonly BIN=".venv/bin"
readonly BASELINE="mutation-baseline.txt"
readonly WORK="mutants"
readonly LOG="${WORK}.log"

update=0
case "${1:-}" in
  "") ;;
  --update) update=1 ;;
  *)
    echo "usage: $0 [--update]" >&2
    exit 2
    ;;
esac

if [[ ! -x "${BIN}/mutmut" ]]; then
  echo "error: ${BIN}/mutmut not found; run: ${BIN}/pip install mutmut==3.8.1" >&2
  exit 2
fi
if [[ ! -f "${BASELINE}" ]]; then
  echo "error: ${BASELINE} missing; run: $0 --update" >&2
  exit 2
fi

tmp_dir="$(mktemp -d)"
trap 'rm -rf "${tmp_dir}"' EXIT
stamp="${tmp_dir}/stamp"
current="${tmp_dir}/current"
baselined="${tmp_dir}/baselined"
touch "${stamp}"

# The copied tests resolve testdata/ relative to the copy. A symlink avoids copying 353 MB per run;
# the suite's own corpus tripwire (testdata/.baseline.md5) still hashes the real files through it.
rm -rf "${WORK}"
mkdir "${WORK}"
ln -s ../testdata "${WORK}/testdata"

echo "mutating and testing (log: ${LOG})..."
if ! "${BIN}/mutmut" run >"${LOG}" 2>&1; then
  echo "error: mutmut run failed; last lines of ${LOG}:" >&2
  LC_ALL=C tr '\r' '\n' <"${LOG}" | grep -v 'Generating mutants' | tail -20 >&2
  exit 2
fi
if LC_ALL=C tr '\r' '\n' <"${LOG}" | grep -q 'failed to collect stats'; then
  echo "error: the unmutated tests do not pass under mutmut; see ${LOG}" >&2
  exit 2
fi

# A mutation run must never write into the pinned corpus.
if [[ -n "$(find testdata -type f -newer "${stamp}" -not -name '.DS_Store' -print -quit)" ]]; then
  echo "error: a file under testdata/ was modified during the run" >&2
  exit 2
fi

"${BIN}/mutmut" results | awk -F': ' '/^ +gps_sync\./ { sub(/^ +/, "", $1); print $1 }' | LC_ALL=C sort >"${current}"

total="$("${BIN}/mutmut" results --all true | grep -c '^ *gps_sync\.' || true)"
echo "mutants: ${total}, not killed: $(wc -l <"${current}" | tr -d ' ')"

if ((update)); then
  tmp="${tmp_dir}/rewritten"
  awk '
    NR == FNR { if ($0 !~ /^#/ && NF) { n = $1; sub(/^[^ ]+ +# /, ""); reason[n] = $0 } ; next }
    { print $1 "  # " ($1 in reason ? reason[$1] : "UNCLASSIFIED") }
  ' "${BASELINE}" "${current}" >"${tmp}"
  {
    awk '/^#/ || !NF' "${BASELINE}"
    cat "${tmp}"
  } >"${BASELINE}.new"
  mv "${BASELINE}.new" "${BASELINE}"
  echo "baseline rewritten: ${BASELINE} (review any UNCLASSIFIED entries)"
  exit 0
fi

awk '/^#/ || !NF { next } { print $1 }' "${BASELINE}" | LC_ALL=C sort >"${baselined}"

new="$(LC_ALL=C comm -23 "${current}" "${baselined}")"
stale="$(LC_ALL=C comm -13 "${current}" "${baselined}")"

if [[ -n "${stale}" ]]; then
  echo "note: baseline entries that no longer survive (safe to delete with --update):"
  # shellcheck disable=SC2001 # prefixing every line is not a single substitution
  sed 's/^/  /' <<<"${stale}"
fi
if [[ -n "${new}" ]]; then
  echo "FAIL: survivors outside the baseline:" >&2
  # shellcheck disable=SC2001 # as above
  sed 's/^/  /' <<<"${new}" >&2
  echo "inspect one with: ${BIN}/mutmut show <name>" >&2
  echo "kill it with a test, or accept it with a reason: $0 --update" >&2
  exit 1
fi
echo "OK: no survivor outside the baseline"
