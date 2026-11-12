# Database Error on Launch or During Use

Portfolio Manager fails to start or displays an error message related to the database. Most database errors are caused by a locked file, a missing directory, or a failed schema migration.

One or more of the following symptoms occur:

-   A dialog shows "Database error" on launch.
-   The application starts but projects, sessions, or milestones do not display.
-   The log file contains `sqlite3.OperationalError` entries.
-   The application crashes when creating or saving data.

**The database directory does not exist or is not writable.**

1.  Verify that `~/.portfolio_manager/` exists and is writable:

    ```
    ls -la ~/.portfolio_manager/
    ```

2.  If the directory does not exist, create it:

    ```
    mkdir -p ~/.portfolio_manager
    ```

3.  Relaunch Portfolio Manager. It will recreate the database automatically.


**A failed schema migration corrupted the database.**

1.  Check whether a backup exists:

    ```
    ls ~/.portfolio_manager/*.bak ~/.portfolio_manager/backups/
    ```

2.  A failed upgrade is rolled back, so your data is normally unchanged and you do not need to restore anything. Relaunch Portfolio Manager. If it fails a second time, check the log file for the specific error and report it to the project repository.

3.  To go back to the copy made just before the upgrade, follow [Back Up and Restore Your Data](t_back_up_and_restore.md) and use the newest `.bak` file.


**The banner says the database is damaged.**

Portfolio Manager checks the database file when it starts. If the check fails, it does not change the file. Restore a backup by following [Back Up and Restore Your Data](t_back_up_and_restore.md).

**The banner says the disk is full or the folder is not writable.**

Free up disk space, or check that `~/.portfolio_manager` is writable, then relaunch.

**The banner says the data was created by a newer version.**

You opened your data with an older copy of Portfolio Manager. Install the latest version. Your data was not changed.


**The database file is locked by another process.**

1.  Check whether another Portfolio Manager instance is already running and close it.

2.  If no other instance is running, verify no orphan process holds the database lock:

    ```
    lsof ~/.portfolio_manager/portfolio.db
    ```

3.  Terminate any process listed, then relaunch Portfolio Manager.


