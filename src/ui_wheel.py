"""Wheel gestures scroll forms; choices are committed explicitly by the user."""
import tkinter as tk


def install_choice_wheel_guard(root):
    if getattr(root,'_ka_choice_wheel_guard',False):return
    root._ka_choice_wheel_guard=True
    def scroll(event):
        widget=event.widget
        if not isinstance(widget,tk.Misc):return 'break'
        delta=getattr(event,'delta',0)
        direction=(-1 if delta>0 else 1) if delta else (-1 if getattr(event,'num',0)==4 else 1)
        units=max(1,int(abs(delta)/120)) if delta else 1
        parent=widget.master
        while parent is not None:
            if isinstance(parent,tk.Canvas) and parent.cget('scrollregion'):
                parent.yview_scroll(direction*units,'units');break
            parent=parent.master
        return 'break'
    for widget_class in ('TCombobox','Spinbox','TSpinbox'):
        for sequence in ('<MouseWheel>','<Shift-MouseWheel>','<Button-4>','<Button-5>'):
            root.bind_class(widget_class,sequence,scroll)
