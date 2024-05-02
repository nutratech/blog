SHELL=/bin/bash

.PHONY: list
list:
	@echo "Listing out existing tags..."
	grep -rh --exclude=Makefile ":tags:" | awk -F":tags:" '{print $$2}' | xargs -I % echo % | tr -s ', ' '\n' | sort | uniq
	@echo
	@echo "Listing out existing categories..."
	grep -rh --exclude=Makefile ":category:" | awk -F":category:" '{print $$2}' | xargs -I % echo % | tr -s  ', ' '\n' | sort | uniq
