.PHONY: test collect update serve sample
test:
	python3 -m unittest discover -s tests -v
collect:
	python3 -m kcdw.collector --output var/snapshot.json
update:
	./scripts/update_report.sh
serve:
	./scripts/serve.sh
sample:
	python3 -m kcdw.renderer tests/fixtures/sample_snapshot.json tests/fixtures/sample_analysis.json --now 2026-09-03T12:10:00Z --output public/index.html --health public/health.json
