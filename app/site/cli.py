"""`flask --app wsgi site export`: build the static (GitHub Pages) edition of the website."""

from __future__ import annotations

import json
from pathlib import Path

import click
from flask import Flask, current_app


def register_site_commands(app: Flask) -> None:
    site = click.Group("site", help="Public website tools.")

    @site.command("export")
    @click.option("--out", default="build/site", show_default=True, help="Output directory (recreated).")
    @click.option("--base-path", default="",
                  help='URL prefix the site is served under, e.g. "/psx-intelligence" for '
                       "https://<user>.github.io/psx-intelligence/ ('' for a root domain).")
    @click.option("--no-news", is_flag=True, help="Skip the per-symbol news files (no Google News requests).")
    @click.option("--report", "report_path", default="", help="Also write the build report (JSON) to this file.")
    def export_cmd(out: str, base_path: str, no_news: bool, report_path: str) -> None:
        """Render every page and its data to static files; fails on any error or broken link."""
        from app.site.export import ExportError, export_site

        try:
            report = export_site(current_app._get_current_object(), Path(out), base_path, with_news=not no_news)
        except ExportError as exc:
            raise click.ClickException(str(exc)) from None
        text = json.dumps(report, indent=2, default=str)
        if report_path:
            Path(report_path).write_text(text)
        click.echo(text)

    app.cli.add_command(site)
