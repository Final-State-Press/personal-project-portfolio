# Back Up and Restore Your Data

Portfolio Manager keeps all of your data in one file on your Mac. Back it up so you can recover from a damaged file, a mistake, or a lost disk.

## Back up

Portfolio Manager makes a backup automatically once a day while you use it, and keeps the newest 7. You can make one yourself at any time.

1.  Click the **Settings** tab.

2.  In the **Backups** section, click **Back up now**.

    The new backup appears in the list, with its date and size. The section also shows the folder that holds them, normally `~/.portfolio_manager/backups/`.


Backups are complete copies of the database, checked for damage when they are made. They are consistent even if you were editing at the time.

Before an upgrade changes your data, Portfolio Manager also makes a copy named `portfolio.<from>-to-<to>.<time>.bak` next to the database, and keeps the newest 5.

For a copy that survives the loss of your Mac, copy the `backups` folder to an external drive or cloud storage now and then. Do not keep the live database file itself in a synced folder such as iCloud Drive or Dropbox. A sync tool can copy the file while it is being written, which corrupts it.

## Export a copy

To keep a copy somewhere else, such as an external drive, click **Export a copy…** in the **Backups** section and choose a folder. Portfolio Manager writes a complete, checked copy named `portfolio-export-<time>.db` there.

## Restore

1.  Click the **Settings** tab.

2.  In the **Backups** section, find the backup you want and click **Restore**. Confirm.

3.  Quit Portfolio Manager and open it again.

    The backup is put in place when the app starts. The data it replaces is kept in the `backups` folder as `before-restore-<time>.db` \(the newest 3 are kept\), so you can undo the restore.


Anything you entered after the backup was made is not in it.

### If the app shows that the database is damaged

The app cannot open the Settings tab in this state. Click **Restore the latest backup** in the red banner, then quit and reopen the app. If there are no backups, or the button does not appear, restore by hand:

1.  Quit Portfolio Manager.

2.  In Finder, choose **Go > Go to Folder** and enter `~/.portfolio_manager`.

3.  Move `portfolio.db` out of the way, for example to your Desktop. Also move `portfolio.db-wal` and `portfolio.db-shm` if they exist.

4.  Open the `backups` folder, copy the backup you want, and paste it into `~/.portfolio_manager`.

5.  Rename the copy to `portfolio.db`.

6.  Open Portfolio Manager.
