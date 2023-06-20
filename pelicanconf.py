"""
Configuration for the site build
"""
import os
from datetime import datetime

AUTHOR = "Shane Jaroch"
SITENAME = "Blog | NutraTech"
SITEURL = os.getenv("NUTRA_BLOG_BASE_URL") or str()

PATH = "content"
# ARTICLE_SAVE_AS = "{date:%Y}/{date:%m}/{slug}.html"
ARTICLE_URL = "{date:%Y}/{date:%m}/{slug}.html"

TIMEZONE = "America/Detroit"

DEFAULT_LANG = "en"

# Feed generation is usually not desired when developing
FEED_ALL_ATOM = None
CATEGORY_FEED_ATOM = None
TRANSLATION_FEED_ATOM = None
AUTHOR_FEED_ATOM = None
AUTHOR_FEED_RSS = None

BROWSER_COLOR = "#333333"
PYGMENTS_STYLE = "monokai"

# Blogroll
LINKS = ()

# Social widget
SOCIAL = (
    ("github", "https://github.com/nutratech"),
    ("facebook", "https://www.facebook.com/nutrallc/"),
)

GITHUB_CORNER_URL = "https://github.com/nutratech"

MENUITEMS = (
    ("Archives", "/archives.html"),
    ("Categories", "/categories.html"),
    ("Tags", "/tags.html"),
)

CC_LICENSE = {
    "name": "Creative Commons Attribution-ShareAlike 4.0 International License",
    "version": "4.0",
    "slug": "by-sa",
    "icon": True,
    "language": "en_US",
}

COPYRIGHT_YEAR = datetime.now().year

DEFAULT_PAGINATION = 10

THEME = "themes/flex"

THEME_COLOR_AUTO_DETECT_BROWSER_PREFERENCE = True
THEME_COLOR_ENABLE_USER_OVERRIDE = True

PLUGINS = [
    "pelican_youtube",
]

# Uncomment following line if you want document-relative URLs when developing
# RELATIVE_URLS = True
