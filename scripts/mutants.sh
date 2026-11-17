#!/usr/bin/env bash
# Mutation gate: mutate the read/classify/match/write path (scope in [tool.mutmut], pyproject.toml),
# run the unit tests against every mutant, and fail on any survivor the committed baseline does not
# admit with a category and a justification.
#
#   scripts/mutants.sh            run the gate
#   scripts/mutants.sh --propose  run, then write a CANDIDATE baseline beside the committed one and
#                                 run the admissions check; never touches mutation-baseline.txt
#   scripts/mutants.sh --promote  replace the committed baseline with a candidate that passed the
#                                 admissions check and has not changed since
#
# The decisions live in mutation_baseline.py, which reads the category column, identifies a mutant
# by a digest of its diff and of its enclosing function (so a renumbered mutant is the same entry
# and a rewritten function is a different one) and admits no survivor in the telemetry module.
# The spinner output has multibyte characters, so tr/sort run under LC_ALL=C (byte semantics).
# Survivors are read from the status column (mutation_baseline.py partition). Every other non-killed
# status is declared there: a timeout counts as detected within a budget, anything else (no tests,
# not checked, suspicious...) fails the gate, and --propose refuses to write an entry for any.
# Always starts from a clean mutants/ tree so a stale cache cannot hide a survivor.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

readonly BIN=".venv/bin"
readonly WORK="mutants"
readonly LOG="${WORK}.log"
readonly DECIDE=("${BIN}/python" scripts/mutation_baseline.py)

mode=gate
case "${1:-}" in
  "") ;;
  --propose) mode=propose ;;
  --promote) mode=promote ;;
  *)
    echo "usage: $0 [--propose | --promote]" >&2
    exit 2
    ;;
esac

if [[ "${mode}" == promote ]]; then
  exec "${DECIDE[@]}" promote
fi

if [[ ! -x "${BIN}/mutmut" ]]; then
  echo "error: ${BIN}/mutmut not found; run: ${BIN}/pip install mutmut==3.8.1" >&2
  exit 2
fi
if [[ ! -f mutation-baseline.txt ]]; then
  echo "error: mutation-baseline.txt missing" >&2
  exit 2
fi

tmp_dir="$(mktemp -d)"
trap 'rm -rf "${tmp_dir}"' EXIT
stamp="${tmp_dir}/stamp"
survivors="${tmp_dir}/survivors"
undetermined="${tmp_dir}/undetermined"
results="${tmp_dir}/results"
all_results="${tmp_dir}/all_results"
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

"${BIN}/mutmut" results >"${results}"
"${BIN}/mutmut" results --all true >"${all_results}"
"${DECIDE[@]}" partition --results "${results}" --all-results "${all_results}" \
  --survivors "${survivors}" --undetermined "${undetermined}"

# Not exec: exec would skip the EXIT trap and leak the temp directory on every run.
status=0
"${DECIDE[@]}" "${mode}" --survivors "${survivors}" --undetermined "${undetermined}" || status=$?
exit "${status}"
