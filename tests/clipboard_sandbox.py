"""Keep test clipboard operations entirely inside each Tcl interpreter."""
from contextlib import contextmanager
import tkinter as tk
from unittest.mock import patch


def isolate_clipboard(root):
    root.tk.eval(r'''
        namespace eval ::ka_test_clipboard {variable text {}; variable present 0}
        rename clipboard ::ka_test_clipboard::unused_native_clipboard
        proc clipboard {action args} {
            variable ::ka_test_clipboard::text
            variable ::ka_test_clipboard::present
            switch -- $action {
                clear {set text {}; set present 0}
                append {append text [lindex $args end]; set present 1}
                get {
                    if {!$present} {error "CLIPBOARD selection does not exist"}
                    return $text
                }
                default {error "unexpected test clipboard operation"}
            }
            return {}
        }
        rename selection ::ka_test_clipboard::native_selection
        proc selection {action args} {
            if {[lsearch -exact $args CLIPBOARD] >= 0} {
                if {$action eq "get"} {return [clipboard get]}
                return {}
            }
            tailcall ::ka_test_clipboard::native_selection $action {*}$args
        }
    ''')


@contextmanager
def isolated_test_clipboards():
    original = tk.Tk.__init__
    def init(root, *args, **kwargs):
        original(root, *args, **kwargs)
        isolate_clipboard(root)
    with patch.object(tk.Tk, '__init__', init):
        yield
