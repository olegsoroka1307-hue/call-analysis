"""Оркестрація: біржа → відсів → чернетка → сповіщення."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from .config import Config
from .errorlog import ErrorLog
from .models import Lead
from .scoring import score_project
from .state import State


@dataclass
class Report:
    fetched: int = 0
    skipped_seen: int = 0
    blocked: Counter = field(default_factory=Counter)
    leads: int = 0
    drafted: int = 0
    notified: int = 0
    errors: int = 0

    def render(self) -> str:
        lines = [
            "",
            "─" * 60,
            f"Переглянуто замовлень:  {self.fetched}",
            f"Уже бачили раніше:      {self.skipped_seen}",
            f"Відсіяно:               {sum(self.blocked.values())}",
        ]
        for reason, count in self.blocked.most_common(4):
            lines.append(f"    · {reason}: {count}")
        lines += [
            f"ПІДХОДЯТЬ:              {self.leads}",
            f"  зі чернеткою відгуку: {self.drafted}",
            f"Помилок:                {self.errors}",
            "─" * 60,
        ]
        return "\n".join(lines)


def run(
    cfg: Config,
    client,
    state: State,
    errlog: ErrorLog,
    notifiers: list,
    drafter=None,
) -> tuple[Report, list[Lead]]:
    report = Report()

    try:
        projects = client.projects(pages=cfg.pages)
    except Exception as exc:
        errlog.record("api", f"не вдалося отримати замовлення: {exc}", exc=exc)
        report.errors = errlog.total
        return report, []

    report.fetched = len(projects)
    leads: list[Lead] = []

    for project in projects:
        if state.seen(project.source, project.project_id):
            report.skipped_seen += 1
            continue

        score = score_project(project, cfg)
        if not score.passed:
            report.blocked[score.blocked_by.split(" —")[0].split(" нижч")[0]] += 1
            # Позначаємо навіть відсіяні: інакше щогодини перебиратимемо те саме.
            state.mark(project.source, project.project_id, score.total, project.title)
            continue

        lead = Lead(project=project, score=score)
        if drafter is not None and score.total >= cfg.draft_min_score:
            try:
                lead.draft = drafter.draft(project, score)
                if lead.draft and not lead.draft.error:
                    report.drafted += 1
                elif lead.draft and lead.draft.error:
                    errlog.record("draft", lead.draft.error)
            except Exception as exc:
                errlog.record("draft", f"збій чернетки: {exc}", exc=exc)

        delivered = False
        for notifier in notifiers:
            try:
                notifier.send(lead)
                delivered = True
            except Exception as exc:
                errlog.record(
                    "notify", f"{notifier.name}: {exc}", exc=exc
                )
        if delivered:
            # Позначаємо лише після того, як хоч кудись доставили,
            # інакше замовлення зникне назавжди через тимчасовий збій.
            state.mark(project.source, project.project_id, score.total, project.title)
            report.notified += 1
        leads.append(lead)
        report.leads += 1

    report.errors = errlog.total
    return report, leads
