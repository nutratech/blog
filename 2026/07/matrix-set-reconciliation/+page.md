---
title: 'Hello World'
date: '2026-07-30'
---

<script>
    // You can import Svelte components here if you want!
    let clicks = 0;
</script>

# {title}

Welcome to your very first blog post! This is written in Markdown but powered by **SvelteKit** and **mdsvex**.

You can even use interactive elements right inside your markdown:

<button on:click={() => clicks++}>
Clicked {clicks} {clicks === 1 ? 'time' : 'times'}
</button>

## Why this is awesome

1. You write content in plain Markdown
2. You get full Svelte interactivity
3. It all builds out to fast static HTML

Pretty cool, right?
