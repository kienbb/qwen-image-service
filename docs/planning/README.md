# Nguồn của tài liệu lập kế hoạch

`BRD.md`, `PRD.md`, `IMPLEMENTATION-PLAN.md` và `TASKS.md` trong `docs/` được sinh từ `plan.json` bằng skill `project-planning`:

```bash
python ~/.claude/skills/project-planning/scripts/generate_docs.py docs/planning/plan.json --out out/ && cp out/*.md docs/
```

Muốn sửa task hay milestone thì sửa `plan.json` rồi sinh lại, không sửa tay các file `.md`. Task mới thêm vào **cuối** mảng `tasks` để ID `QIS-###` không bị đánh lại.
