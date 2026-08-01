SHELL=/bin/bash

.PHONY: format
format:
	prettier --write .
	-markdownlint $$(git ls-files '*.md')
