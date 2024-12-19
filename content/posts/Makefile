# SHELL=/bin/bash

# BLOG_TAGS ?=
# BLOG_CATS ?=

.PHONY: list
list:
	@echo "Listing out existing tags..."
	grep -rh --exclude=Makefile ":tags:" | awk -F":tags:" '{print $$2}' | xargs | tr -s ', ' '\n' | sort | uniq -c
	@echo
	@echo "Listing out existing categories..."
	grep -rh --exclude=Makefile ":category:" | awk -F":category:" '{print $$2}' | xargs | tr -s  ', ' '\n' | sort | uniq -c
