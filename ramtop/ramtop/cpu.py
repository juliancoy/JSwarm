"""CPU time accounting for the htop-style utilization panel."""
from dataclasses import dataclass
import psutil
from rich.table import Table
from rich.text import Text


@dataclass
class CpuUsage:
    user: float
    system: float
    nice: float
    steal: float
    wait: float

    @property
    def busy(self):
        return self.user + self.system + self.nice + self.steal


def utilization(previous, current):
    """Linux guest time is already included in user/nice; do not count it twice."""
    before, after = previous._asdict(), current._asdict()
    delta = {key: value - before.get(key, value) for key, value in after.items()
             if key not in ('guest', 'guest_nice')}
    total = sum(delta.values())
    if total <= 0 or any(value < 0 for value in delta.values()):
        return None
    def percent(*fields):
        return sum(delta.get(key, 0) for key in fields) * 100 / total
    return CpuUsage(percent('user'), percent('system', 'irq', 'softirq'),
                    percent('nice'), percent('steal'), percent('iowait'))


class CpuMonitor:
    def __init__(self):
        self.previous = psutil.cpu_times(percpu=True)
        self.usages = [None] * len(self.previous)

    def sample(self):
        current = psutil.cpu_times(percpu=True)
        self.usages = [utilization(self.previous[index], value) if index < len(self.previous) else None
                       for index, value in enumerate(current)]
        self.previous = current
        return self.usages


def cpu_bar(index, usage, width):
    label = 'AVG' if index is None else f'{index:3}'
    text = Text(f'{label} [', style='bold')
    if usage is None:
        text.append(' ' * width + ']  N/A', style='dim')
        return text
    filled = 0
    for value, color in [(usage.user, 'green'), (usage.system, 'red'), (usage.nice, 'blue'),
                         (usage.steal, 'yellow'), (usage.wait, 'cyan')]:
        count = min(width - filled, max(0, round(value * width / 100)))
        text.append('|' * count, style=color)
        filled += count
    text.append(' ' * (width - filled) + '] ')
    text.append(f'{usage.busy:5.1f}%', style='bold white')
    return text


def cpu_display(usages, width=80):
    columns = 2 if width >= 75 else 1
    bar_width = max(8, min(40, width // columns - 17))
    table = Table.grid(expand=True, padding=(0, 2))
    for _ in range(columns):
        table.add_column(ratio=1)
    valid = [usage for usage in usages if usage is not None]
    average = CpuUsage(*(sum(getattr(value, field) for value in valid) / len(valid)
                         for field in ('user', 'system', 'nice', 'steal', 'wait'))) if valid else None
    table.add_row(cpu_bar(None, average, bar_width), *([''] if columns == 2 else []))
    for index in range(0, len(usages), columns):
        cells = [cpu_bar(cpu, usages[cpu], bar_width) for cpu in range(index, min(index + columns, len(usages)))]
        table.add_row(*cells, *([''] if len(cells) < columns else []))
    return table
