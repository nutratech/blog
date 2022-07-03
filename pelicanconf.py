"""
Configuration for the site build
"""
AUTHOR = "Shane J"
SITENAME = "Modern Trends"
SITEURL = ""

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

# THEME = "/home/shane/.pelican-themes/pelican-cait"
THEME = "/home/shane/.pelican-themes/elegant"

# Uncomment following line if you want document-relative URLs when developing
# RELATIVE_URLS = True
