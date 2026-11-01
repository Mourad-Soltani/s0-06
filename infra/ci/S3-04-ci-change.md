# S3-04: the two CI changes this kit needs

The new tests live in `ui/tests/` and `orchestrator/tests/`. `orchestrator` is already collected; `ui` is NOT, and the Streamlit
tests need `streamlit`. Edit `.github/workflows/ci.yml` (job `pytest`):

1. After the step that installs `requirements.txt`, install the dashboard's dependency:

       pip install -r ui/requirements.txt

2. Add `ui` to the list of paths given to pytest (next to `adapters`, `contracts/tests` and `orchestrator`).

Nothing else changes: the gate, the mutation steps and the required check keep their names.

How to tell it worked: the collected count rises by the number in the kit report. If it does not, `ui` was not added. If the run
fails at collection with `No module named 'streamlit'`, the install step was not added.
