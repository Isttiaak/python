# Daily ERP course enrollment on GitHub Actions

GitHub starts a fresh Linux server once a day and runs `enroll.py`. The script
reads the master sheet and every advisor's student sheet, signs in to the ERP,
and enrolls only the students who weren't enrolled on an earlier day. Then the
server shuts down. Your computer can stay off.

```
enroll.py                              the script
requirements.txt                       Python packages it needs
.github/workflows/daily-enrollment.yml when and how GitHub runs it
```

## Before you start

**Share the master sheet and every advisor's sheet as "Anyone with the link: Viewer".**
Otherwise the script can't download it.

Academic Year and Semester are read from the master sheet, row by row. The ERP
shows them as dropdown lists, and the script picks the matching item itself:
`2026-27`, `26-27` and `2026/2027` all become `2026-2027`; `Fall 2026` becomes
`Fall`; `Fall'26` becomes `Fall 2026`. If the ERP's semester list includes the
year, the year column decides it. The one thing it won't do is guess. A value
that fits two ERP items (a bare `2026` when the ERP has both `2025-2026` and
`2026-2027`) or none at all is skipped, and that run is marked failed. The log
lists the ERP's items, so you can correct that cell in the master sheet, and the
next run picks those students up.

## Step 1: Create a private repository

On github.com, click **+** (top right), then **New repository**.

- Repository name: `erp-enrollment`
- Choose **Private**. This is not optional. On a public repository, anyone can
  read the run logs, and any signed-in GitHub user can download the run files.
  Both contain student IDs and emails. The workflow refuses to run in a public
  repository.
- Tick **Add a README file**.
- Click **Create repository**.

## Step 2: Upload the script

In the new repository, click **Add file**, then **Upload files**. Drag in
`enroll.py` and `requirements.txt`, then click **Commit changes**.

## Step 3: Add the workflow file

Click **Add file**, then **Create new file**.

- In the file name box, type `.github/workflows/daily-enrollment.yml`. Each `/`
  you type creates a folder.
- Paste the whole content of `daily-enrollment.yml`.
- Click **Commit changes**.

## Step 4: Add the three secrets

Go to **Settings > Secrets and variables > Actions > New repository secret**
and add three secrets:

| Name           | Value                                   |
|----------------|-----------------------------------------|
| `MASTER_LINK`  | the master sheet link                   |
| `ERP_ID`       | your ERP employee ID                    |
| `ERP_PASSWORD` | your ERP password                       |

Secrets are encrypted, and GitHub hides them in logs.

## Step 5: Do a test run (dry run)

1. Open the **Actions** tab. If GitHub shows a button to enable workflows, click it.
2. Click **Daily course enrollment** on the left, then **Run workflow**.
3. Leave **Dry run** ticked and click the green **Run workflow** button.
4. After about a minute a run appears. Click it, then click **enroll**, then open the **Enroll** step.

Check these things:

- `Signing in as ...` is followed by the term lines, with no `LOGIN FAILED`.
- The roster audit lists the right sheets and students.
- Every student ends with `DRY_RUN`. That means the script reached the Enroll
  Course button and didn't click it.

At the bottom of the run page, under **Artifacts**, you can download
`combined_roster.csv`, the log, and screenshots of any failures.

**If you see `LOGIN FAILED` or a timeout while opening the login page**,
check your credentials first. If they're right, the ERP probably blocks
GitHub's servers (they are in the USA). Fixing that isn't something this setup
can do. You'd need a machine that the ERP accepts: a GitHub self-hosted runner
on a computer or VPS in Bangladesh.

## Step 6: Do the first real run

Click **Run workflow** again, untick **Dry run**, and run it. Afterwards,
open two or three of the enrolled students in the ERP and check them by hand.

## Step 7: Let it run every day

After that you don't need to do anything. The workflow runs every day at
**08:17 Bangladesh time**. GitHub can start a scheduled run late, sometimes by
more than half an hour when its servers are busy.

- **Results:** each run page shows a summary table. It has counts only, no student data.
- **Failures:** GitHub emails you when a run fails. A run fails when a sheet link
  can't be read, a term doesn't match the ERP, a student errors, or sign-in fails.
  The log says which, and names the advisor.
- **Change the time:** edit the `cron:` line in `daily-enrollment.yml`. It is in
  UTC, which is Bangladesh time minus 6 hours. For example, `"45 13 * * *"`
  means 19:45 Bangladesh time.
- **Pause or stop:** go to Actions > Daily course enrollment, click **...**, then
  **Disable workflow**. Use **Enable workflow** to start it again.
- **Change your password:** update the `ERP_PASSWORD` secret.

## Good to know

- **Free minutes:** private repositories get 2,000 free Actions minutes a month.
  The first full run takes roughly 10 seconds per student. Later runs only
  process new students, so they take a few minutes.
- **The "already done" list** is kept in the Actions cache between runs. GitHub
  deletes a cache that isn't used for 7 days. If the workflow is paused longer
  than that, the next run checks everyone again. It then depends on the ERP's
  "already enrolled" message to skip students who are done.
- **If something goes wrong mid-run:** after 5 errors in a row (the ERP is down,
  or its page changed) the run stops instead of trying every student. If your
  ERP session expires during a run, the script signs in again.
