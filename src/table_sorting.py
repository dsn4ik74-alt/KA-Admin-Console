"""Sort table rows using raw values, keeping selection and displayed units intact."""
import math


def text_key(value):
    # Put Russian ё after е, rather than after я as in Unicode code point order.
    return str(value).casefold().replace('ё', 'е\U0010ffff')


class TableSorter:
    def __init__(self, tree, numeric=(), colors=None):
        self.tree, self.numeric = tree, set(numeric)
        self.column, self.reverse, self.values = None, False, None
        self.titles = {column: tree.heading(column, 'text') for column in tree['columns']}
        if colors:
            tree.tag_configure('even', background=colors['panel'])
            tree.tag_configure('odd', background=colors['row'])
        self.striped = colors is not None
        self.update_headings()

    def update_headings(self):
        for column, title in self.titles.items():
            arrow = ('  ↓' if self.reverse else '  ↑') if column == self.column else ''
            self.tree.heading(column, text=title + arrow, command=lambda key=column: self.toggle(key))

    def toggle(self, column):
        self.reverse = not self.reverse if column == self.column else False
        self.column = column
        self.update_headings()
        self.apply()

    def apply(self, values=None):
        if values is not None:
            self.values = values
        rows = list(self.tree.get_children())
        if self.column:
            known, missing = [], []
            for row in rows:
                value = (self.values.get(row, {}).get(self.column) if self.values is not None else
                         self.tree.set(row, self.column))
                if self.column in self.numeric and value is not None:
                    try:
                        value = float(value)
                        if not math.isfinite(value):
                            value = None
                    except (TypeError, ValueError):
                        value = None
                if value is None or value == '' or value == '—':
                    missing.append(row)
                else:
                    known.append((row, value if self.column in self.numeric else text_key(value)))
            known.sort(key=lambda item: item[1], reverse=self.reverse)
            rows = [row for row, _ in known] + missing
        for position, row in enumerate(rows):
            self.tree.move(row, '', position)
            if self.striped:
                tags = tuple(tag for tag in self.tree.item(row, 'tags') if tag not in ('odd', 'even'))
                self.tree.item(row, tags=tags + ('odd' if position % 2 else 'even',))
