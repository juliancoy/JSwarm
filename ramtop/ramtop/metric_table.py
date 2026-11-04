from rich.text import Text
from textual.widgets import DataTable


class MetricTable(DataTable):
    """Sortable raw values with human-readable cells; missing data always sorts last."""
    def __init__(self, columns, *, sort=0, reverse=False, **kwargs):
        super().__init__(zebra_stripes=True, cursor_type='row', **kwargs)
        self.spec = columns
        self.sort_index, self.reverse = sort, reverse
        self.records = []

    def on_mount(self):
        self.add_columns(*(label for label, _ in self.spec))

    def set_records(self, records):
        self.records = records
        self.render_records()

    def on_data_table_header_selected(self, event):
        event.stop()
        index = event.column_index
        self.reverse = not self.reverse if self.sort_index == index else self.spec[index][1] != 'text'
        self.sort_index = index
        self.render_records()

    def render_records(self):
        from .app import size
        cursor = self.cursor_row
        self.clear()
        valid = [row for row in self.records if row[self.sort_index] is not None]
        valid.sort(key=lambda row: row[self.sort_index].casefold() if isinstance(row[self.sort_index], str)
                   else row[self.sort_index], reverse=self.reverse)
        rows = valid + [row for row in self.records if row[self.sort_index] is None]
        for index, (column, (label, _)) in enumerate(zip(self.ordered_columns, self.spec)):
            column.label = Text(label + (' ↓' if self.reverse else ' ↑') if index == self.sort_index else label)
            column.content_width = max(column.content_width, len(column.label))
        for row in rows:
            cells = []
            for value, (_, kind) in zip(row, self.spec):
                if value is None:
                    cell = 'N/A'
                elif kind in ('bytes', 'rate'):
                    cell = size(value) + ('/s' if kind == 'rate' else '')
                elif kind == 'percent':
                    cell = f'{value:.1f}%'
                elif kind == 'float':
                    cell = f'{value:,.2f}'
                else:
                    cell = str(value)
                cells.append(Text(cell))
            self.add_row(*cells)
        if self.row_count:
            self.move_cursor(row=min(cursor, self.row_count-1), scroll=False)
        self.refresh(layout=True)
