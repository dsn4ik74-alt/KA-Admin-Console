"""Coalesce expensive text geometry changes during native window resizing."""


def delayed_wrap(label, minimum=100, inset=0):
    state = {'job': None, 'width': None}
    def apply():
        state['job'] = None
        width = state['width']
        if label.winfo_exists() and width is not None and int(label.cget('wraplength')) != width:
            label.configure(wraplength=width)
    def resized(event):
        width = max(minimum, event.width - inset)
        if width == state['width']:
            return
        state['width'] = width
        if state['job'] is not None:
            label.after_cancel(state['job'])
        state['job'] = label.after(70, apply)
    def destroyed(event):
        if event.widget == label and state['job'] is not None:
            label.after_cancel(state['job'])
            state['job'] = None
    label.bind('<Configure>', resized)
    label.bind('<Destroy>', destroyed, add='+')
