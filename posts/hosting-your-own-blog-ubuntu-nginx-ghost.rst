**************************************************
 Hosting your own blog with ghost, Ubuntu & nginx
**************************************************

.. post:: 01, July 2022
    :tags: development
    :author: shane

This is one of the most affordable ways to host your own content.
It's also one of the most nuanced.

**NOTE:** My setup also involves a web application, an API, database,
and a few other services.
But you can start with a single service, e.g. a blog.

Using the best tools, this stack runs comfortably on limited hardware.
A 20.04 Ubuntu server will idle around 400 MB of RAM, and 2% on a 2-core CPU.

For my website https://nutra.tk/api/, I have the following routes defined
in nginx.

::

    /      -->  Website (User Interface)
    /api   -->  Server application
    /blog  -->  Blog (Ghost / WordPress)

Configuring your Ubuntu Server
##############################

.. important::

    This guide assumes you have an Ubuntu server with working SSH access
    and basic Linux experience.
    Hosting a basic VPS costs in the range of $3 - $10 / month.

You will need SSH and root access.

You will need to know the public IP address of your Ubuntu server.
You can check this in the following ways.

See: https://www.makeuseof.com/get-public-ip-address-in-linux/

Setting up Ghost
################

.. important::

    You can find the official guide here. I recommend comparing it with
    my steps and deciding what works for your situation.
    I am using sqlite3 for simplicity.

You will need to install NodeJS and then the Ghost CLI.
You can install NodeJS using the guide here.

Then you will need to install the Ghost CLI.

.. code-block:: text

    sudo npm install --location=global ghost-cli@latest

Now create a folder and install the ghost blog there.

.. code-block:: bash

    sudo mkdir -p /var/www/blog
    sudo chown $LOGNAME:$LOGNAME /var/www/blog
    chmod 775 /var/www/blog

    cd /var/www/blog
    ghost install --db=sqlite3
    ghost setup

Set the url to the real url for that server, e.g. https://example.com/blog

Choose to start ghost. Then verify it is running with these commands.

.. code-block:: bash

    ghost ls
    curl localhost:2368

Domain registration & DNS records
#################################

.. note::

    This site offers free ``.tk`` domains.
    `Freenom - A Name for Everyone <https://www.freenom.com/en/index.html?lang=en>`_


You need to register a domain and point its ``"A"`` records to your
Ubuntu server's IP.

1. Register for an account
2. Go to ``Services > Register`` a New Domain, and complete the steps.
3. Go to ``Services > My Domains``, and click *"Manage"* for your domain
4. Click the last tab, *"Manage Freenom DNS"*


Now using the IP address of your Ubuntu server as the ``Target``,
add two ``A`` records.
One with a blank Name, and one with ``WWW``. The ``Target`` for both should
be your Ubuntu server's IP.

Click save changes and start the next steps. It will take 10 - 15 minutes
for the DNS server to fully refresh its cache.

Basic routing in nginx
######################

.. important::

    For editing files, I'll be using vim.
    You can also use nano, or switch editors with
    ``sudo update-alternatives --config editor``

We'll be working on the VPS again for this step.
You'll want to install the ``nginx`` Debian package,
as well as the ``CertBot`` snap.

.. code-block:: bash

    sudo apt install nginx
    sudo snap install certbot --classic

Enable ufw and make firewall exceptions.

.. code-block:: bash

    sudo ufw enable
    sudo ufw allow "Nginx Full"
    sudo ufw allow OpenSSH

Now you can enable your site availability in the nginx config.

sudo vim /etc/nginx/sites-available/default

And update it as follows.
You will need to replace ``nutra.tk`` with your domain name.
Since we are already running ghost on our VPS at port 2368,
our configuration will look like this.

.. code-block:: nginx

    server {
      server_name nutra.tk;
      listen [::]:443 ssl ipv6only=on;
      listen 443 ssl;

      # Ghost
      client_max_body_size 50m;
      root /var/www/blog/system/nginx-root; # Used for acme.sh SSL verification (https://acme.sh)

      location ^~ /blog/ {
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header Host $http_host;
        proxy_pass http://127.0.0.1:2368;
        proxy_redirect off;
      }

      location ~ /.well-known {
        allow all;
      }

      # default favicon
      location = /favicon.ico {
        alias /var/www/favicon.gif;
      }
    }


    # Redirect all HTTP to HTTPS with no-WWW
    server {
      listen 80 default_server;
      listen [::]:80 default_server;
      server_name ~^(?:www\.)?(.*)$;
      return 301 https://$1$request_uri;
    }


    # Redirect WWW to no-WWW
    server {
      listen 443 ssl http2;
      listen [::]:443 ssl http2;
      server_name ~^www\.(.*)$;
      return 301 $scheme://$1$request_uri;
    }

If you don't want to have the ``/blog`` on the end of your URL,
you can use your homepage as the blog.
Simply replace ``^~ /blog/`` with ``/``.

To test your changes and reload nginx, run this.

.. code-block:: bash

    sudo nginx -t sudo nginx -s reload

Now your blog should be public at your domain URL.

**NOTE:** You may wish to copy a (small 32x32) ``GIF`` display icon
into the location ``/var/www/favicon.gif``

**NOTE:** Bonus points if you manage to install ``git``, and initialize a repo
at the root ``/.git``, keeping track of any changes in the nginx
default file and related configs.

HTTPS and CertBot
=================

Next we need to enable ``HTTPS`` and ``SSL`` verification, which is a
requirement of most modern browsers and tools.

**NOTE:** Replace example.com with your website.

.. code-block:: bash

    sudo certbot \
        --nginx \
        --key-type ecdsa \
        --preferred-chain "ISRG Root X1" \
        -d example.com

Open up the ``sites-available/default`` config file and investigate it for any
suspicious automated changes. Perform an ``nginx -s reload``, and test out
your website to see if everything still works.


Backups (and other words of caution)
####################################

Self-hosting can be tough. You need to back up regularly,
and any writing,any comments or media uploaded in between is precarious.
If anything happens to your VPS, you may be only able to restore as recently
as your last backup point.

One option is to register a ``cronjob`` (on your personal machine),
which performs a secure copy command twice a day.
You can then perform weekly compressions and store to Google drive or run
``rsync`` on a large hard disk of your own.

An application like this also won't scale to millions of views per day
without heavily tweaking, adding, and improving things.

But it is a solid starting point, and can handle more requests than most
websites will see.
