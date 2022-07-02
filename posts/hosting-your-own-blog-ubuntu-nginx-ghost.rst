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
https://www.makeuseof.com/get-public-ip-address-in-linux/

Setting up Ghost
################

.. important::

    You can find the official guide here. I recommend comparing it with
    my steps and deciding what works for your situation.
    I am using sqlite3 for simplicity.

You will need to install NodeJS and then the Ghost CLI.
You can install NodeJS using the guide here.

Then you will need to install the Ghost CLI.

::

    sudo npm install --location=global ghost-cli@latest

Now create a folder and install the ghost blog there.

::

    sudo mkdir -p /var/www/blog
    sudo chown $LOGNAME:$LOGNAME /var/www/blog
    chmod 775 /var/www/blog

    cd /var/www/blog
    ghost install --db=sqlite3
    ghost setup

Set the url to the real url for that server, e.g. https://example.com/blog

Choose to start ghost. Then verify it is running with these commands.

::

    ghost ls
    curl localhost:2368
