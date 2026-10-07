PY ?= .venv/bin/python

.PHONY: test eval release

test:
	$(PY) -m pytest -q

# Rebuild rules from cached model responses, then evaluate. Fails if any threshold is missed.
eval:
	$(PY) eval.py --reextract --offline

# Tag a release only if tests and the eval gate pass on a clean tree:  make release VERSION=0.1.0
release:
	@test -n "$(VERSION)" || (echo "usage: make release VERSION=x.y.z" && exit 2)
	@test -z "$$(git status --porcelain)" || (echo "refusing to release: uncommitted changes" && exit 2)
	$(PY) -m pytest -q
	$(PY) eval.py --reextract --offline || (echo "refusing to release: eval gate failed (see eval_report.md)" && exit 1)
	git tag -a v$(VERSION) -m "v$(VERSION)"
	@echo "tagged v$(VERSION). push with: git push origin v$(VERSION)"
