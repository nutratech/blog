"""
Configuration for the site build
"""
import os

AUTHOR = "Shane Jaroch"
SITENAME = "Blog | NutraTech"
SITEURL = os.getenv("NUTRA_BLOG_BASE_URL") or str()

PATH = "content"

TIMEZONE = "America/Detroit"

DEFAULT_LANG = "en"

# Feed generation is usually not desired when developing
FEED_ALL_ATOM = None
CATEGORY_FEED_ATOM = None
TRANSLATION_FEED_ATOM = None
AUTHOR_FEED_ATOM = None
AUTHOR_FEED_RSS = None

# Blogroll
LINKS = ()

# Social widget
SOCIAL = (
    ("GitHub", "https://github.com/nutratech"),
    ("Facebook", "https://www.facebook.com/nutrallc/"),
)

DEFAULT_PAGINATION = 10

THEME = "themes/elegant"

PLUGINS = [
    "pelican_youtube",
]

# Uncomment following line if you want document-relative URLs when developing
# RELATIVE_URLS = True
