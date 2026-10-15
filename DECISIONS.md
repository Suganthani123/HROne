# Design Decisions

1. **Indexes.** A unique `emp_code` index prevents duplicate employees. `(department, emp_code)` supports sorted department lists, and joined-date indexes support headcount. Attendance has a unique `(emp_code, date)` index for daily records. Other indexes support attendance lists, punch-out lookup, and monthly late ranking.
2. **Punch-in race.** Two requests may read the employee at once, but the unique attendance index lets only one insert win. The winner returns 201; the duplicate-key loser returns 409. The database constraint also works across multiple app workers.
3. **Ties.** MongoDB `$rank` gives tied totals the same rank and skips the next rank. Filtering on `rank <= limit` happens afterward, so all employees tied at the cutoff are returned. Results are then sorted by lateness and employee code.
4. **Headcount.** The summary starts from employees who joined by month-end, then looks up their logs. Employees without logs still count; later joiners do not. Starting from attendance would omit employees with no logs.
5. **100x data.** I would first measure latency and explain plans on representative data. Then I would tune proven query or index bottlenecks and consider daily analytics summaries if needed, keeping raw logs as the source of truth.
