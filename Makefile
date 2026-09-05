# Rebuild everything that is not in the repository.
#
# `pages/`, `retry/`, `assets/` and `smilies/` are the archaeology and are
# versioned; `forum.db` and `site/` are not — they come back from the pages in
# a couple of minutes, so a clone plus `make` gives a browsable copy.
#
#   make            db + site + search index
#   make db         pages/ -> forum.db              (~2 min)
#   make site       forum.db -> site/               (~25 s, 6634 pages)
#   make search     site/ -> site/pagefind/         (~30 s)
#   make serve      browse it at http://localhost:8000/
#   make clean      throw away both artifacts

PYTHON   ?= python3
PAGEFIND ?= ./bin/pagefind
DB       ?= forum.db
SITE     ?= site
BASE_URL ?= https://vjt.github.io/azzurra-forum-archive/
PORT     ?= 8000

.PHONY: all db site search serve clean check

all: search

db: $(DB)

# The importer walks every page, so the stamp is the pages directory itself.
#
# Three steps and not one: vBulletin first, then the phpBB mirror into its own
# staging tables, then the merge that folds the second into the first.  The
# corpus is single — it is the same forum, it only changed software — and a
# rebuild that stopped after the first step would silently drop 5629 posts.
$(DB): forum_import.py oldboard_import.py oldboard_merge.py \
       $(shell find pages retry oldboard -maxdepth 1 -type d 2>/dev/null)
	$(PYTHON) forum_import.py --db $(DB)
	$(PYTHON) oldboard_import.py --db $(DB)
	$(PYTHON) oldboard_merge.py --db $(DB)

site: $(SITE)/index.html

$(SITE)/index.html: forum_render.py $(DB)
	$(PYTHON) forum_render.py --db $(DB) --out $(SITE) --base-url "$(BASE_URL)"

# PAGEFIND can be a path (`./bin/pagefind`, the default) or a command — CI sets
# `PAGEFIND="npx -y pagefind"` and there is no binary to check for, so the test
# is "does it run", not "is it an executable file".
search: $(SITE)/pagefind/pagefind.js

$(SITE)/pagefind/pagefind.js: $(SITE)/index.html
	@$(PAGEFIND) --version >/dev/null 2>&1 || { \
	  echo "$(PAGEFIND) does not run — either drop a release binary from"; \
	  echo "https://github.com/CloudCannon/pagefind into bin/, or build with"; \
	  echo "  make search PAGEFIND='npx -y pagefind'"; exit 1; }
	$(PAGEFIND) --site $(SITE) --output-subdir pagefind

serve: all
	@echo "http://localhost:$(PORT)/"
	@cd $(SITE) && $(PYTHON) -m http.server $(PORT)

# The numbers the README quotes. Re-run after any importer change.
check: $(DB)
	@sqlite3 $(DB) \
	  "SELECT 'posts', count(*) FROM posts;" \
	  "SELECT 'truncated', count(*) FROM posts WHERE truncated = 1;" \
	  "SELECT 'threads', count(*) FROM threads;" \
	  "SELECT 'head-only threads', count(*) FROM threads WHERE post_count = 0;" \
	  "SELECT source, count(*) FROM posts GROUP BY source;"

clean:
	rm -rf $(SITE) $(DB) $(DB)-wal $(DB)-shm
