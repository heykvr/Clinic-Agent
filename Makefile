PY ?= python

install:            ## create venv deps
	$(PY) -m pip install -e ".[dev]"

chat:               ## talk to the agent (current policy)
	$(PY) -m clinic_agent.chat --trace

eval:               ## score current policy: all scenarios x 3 trials
	$(PY) -m clinic_agent.evals.runner --trials 3

loop:               ## run the improvement loop from policies/v0.yaml
	$(PY) -m clinic_agent.evals.loop --policy policies/v0.yaml --iterations 2 --trials 3

calibrate:          ## judge agreement with hand labels
	$(PY) -m clinic_agent.evals.calibrate_judge --repeats 2

test:               ## offline tests (no API keys needed)
	$(PY) -m pytest -q

reset:              ## discard loop-produced policies, point CURRENT back at v0
	rm -f policies/v[1-9]*.yaml && echo v0.yaml > policies/CURRENT
