"""Row-level scoping helpers — teacher 只看自己班的學生等列級權限的唯一實作來源。

新增 scope 時在此加入對應 module 的 re-export，確保呼叫端只需
`from services.scoping import <module_name>` 即可取用。
"""

from . import student_scope

__all__ = ["student_scope"]
