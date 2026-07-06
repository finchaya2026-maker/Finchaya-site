# FinChaya — starter site

A static site for finchaya.com: homepage, articles, and calculators.
No build tools, no dependencies — plain HTML + one shared CSS file.

## What's in here

```
finchaya-site/
├── index.html                     Homepage (article list + tools grid)
├── about.html                     About + disclaimer page
├── style.css                      Shared design for every page
├── articles/
│   ├── sip-vs-lumpsum.html        Sample first article (edit or replace)
│   └── _article-template.html     Copy this to write a new article
└── tools/
    └── expected-returns.html      The Expected Returns calculator
```

## Deploy for the first time (one-time, ~20 minutes)

1. **GitHub** — create a free account at github.com, then a new repository
   (e.g. `finchaya-site`). Click "uploading an existing file" and drag the
   *contents* of this folder in (index.html must sit at the repo root, not
   inside a subfolder). Commit.

2. **Cloudflare Pages** — sign up free at dash.cloudflare.com →
   Workers & Pages → Create → Pages → Connect to Git → pick your repo.
   Build command: *leave empty*. Output directory: `/`. Deploy.
   You'll get a preview URL like `finchaya-site.pages.dev` — check it works.

3. **Custom domain** — in the Pages project → Custom domains → add
   `finchaya.com`. Follow the prompt to add your domain to Cloudflare, then
   change the **nameservers** at your domain registrar to the two Cloudflare
   shows you. Also add `www.finchaya.com`. HTTPS is automatic.
   (Nameserver changes can take a few minutes to a few hours.)

## Publish a new article (every time)

1. Copy `articles/_article-template.html` → rename it, e.g.
   `articles/why-expense-ratios-matter.html`.
2. Fill in the title, description, date, and body (the template lists all
   the building blocks you need).
3. Open `index.html` and add a `<li>…</li>` entry at the TOP of the
   "Latest articles" list, pointing to your new file.
4. Upload both changed files to GitHub. The live site updates itself
   within a minute.

## Add the live NAV calculator later

When you've picked your four funds and set their scheme codes, save the
live calculator as `tools/actual-returns.html` and un-comment its card in
`index.html` (it's already there, marked with a comment).

## Notes

- The `_article-template.html` file starting with `_` is fine to leave in
  the repo; nothing links to it.
- Keep owner-only tools (like the scheme-code finder) out of this repo,
  or in a privately named path.
- All content is educational; the disclaimer lives on about.html and in
  every footer.
