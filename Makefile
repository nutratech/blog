SHELL=/bin/bash

.PHONY: format
format:
	prettier --prose-wrap always --print-width 80 --write .
	-markdownlint $$(git ls-files '*.md')
