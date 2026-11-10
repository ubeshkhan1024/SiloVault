# SiloVault M1 — Manual Browser Test Procedure

**Prerequisites:** Backend server running on `http://127.0.0.1:8000`  
**Frontend URL:** `http://localhost:3000`

---

## Test Procedure

### 1. Dashboard Loading
- [ ] Open `http://localhost:3000` in browser
- [ ] Page loads without console errors (F12 → Console)
- [ ] Sidebar shows all 6 navigation items (Overview, Files, Uploads, Sync Status, Activity, Settings)
- [ ] Overview view is active by default
- [ ] "Indexed Files" stat shows a number (may be 0 for empty DB)

### 2. Files View — Empty State
- [ ] Click **Files** in sidebar
- [ ] View title shows "Files"
- [ ] Panel body shows placeholder message when no files exist: *"No indexed files yet."* or similar
- [ ] No JavaScript errors in console

### 3. Upload a File
- [ ] Click **Uploads** in sidebar
- [ ] Drag-and-drop zone is visible
- [ ] Click the upload zone OR use "+ Upload" button in topbar
- [ ] Select a small text file (e.g., create `test.txt` with content "hello silovault")
- [ ] Queue entry appears with status indicator ("Uploading..." / "Uploaded" / "Failed")
- [ ] After upload completes, Files view should show the new record when you switch to it
- [ ] Record displays: original filename, provider = LOCAL, size, MIME type

### 4. Refresh and Confirm Metadata
- [ ] Click **Files** again
- [ ] Your uploaded file appears in the list
- [ ] Click/expand the row to see full metadata:
  - Name matches uploaded filename
  - Provider shows "LOCAL"
  - Size in bytes is correct
  - MIME type detected correctly
  - Sync state badge shows (LOCAL/PENDING)
- [ ] Copy the `file_xxx` ID from the URL bar if viewing detail (or note it from the list)

### 5. Download and Verify Content
- [ ] In Files view, click the **Download** button for your uploaded file
- [ ] Browser prompts to save/download the file
- [ ] Open the downloaded file
- [ ] Content matches original upload byte-for-byte
- [ ] Filename matches original (not the UUID storage name)

### 6. Delete with Confirmation
- [ ] Back in Files view, click the **Delete** button next to your file
- [ ] Native `window.confirm()` dialog appears asking "Are you sure...?"
- [ ] Click **Cancel** — file remains in the list, row stays visible
- [ ] Click **Delete** again, then **OK** in confirm dialog
- [ ] Row is removed from the DOM immediately
- [ ] Switch away and back to Files tab — file no longer appears in list
- [ ] Try opening a detail URL with the deleted file's ID directly — should return 404 page or show error

### 7. Error Handling — Missing File
- [ ] Navigate to `http://localhost:3000` and open DevTools (F12)
- [ ] Go to Network tab, filter by XHR/Fetch
- [ ] Manually trigger a request to `/api/v1/files/file_does_not_exist_12345` (use Console or Network inspector)
- [ ] Response should be HTTP 404 with JSON `{detail: "File with id '...' was not found."}`
- [ ] The Files view should show an appropriate error state (not crash the app)

---

## Expected Console Output (Clean Run)

```
No errors in Console tab
No failed requests in Network tab (except intentional 404 test)
CORS preflight returns 200 for OPTIONS requests
```

---

## Cleanup After Testing

When finished:
1. Close both backend and frontend servers (Ctrl+C in each terminal)
2. Delete the isolated test directory:
   ```powershell
   Remove-Item "$env:TEMP\silovault_browser_test_*" -Recurse -Force -ErrorAction SilentlyContinue
   ```
   This removes the temp SQLite database and uploads — **your real `silo_vault.db` is untouched**.

---

## Known Limitations (Acceptable)

- **Sync Status, Activity, Settings views** — these are placeholder stubs (M2+ features). They will show "placeholder" messages. Do not expect functionality here.
- **Dashboard stats** — may show stale or zero values until actual data exists.
- **Single-tab testing only** — test one scenario at a time per session; don't run concurrent browsers.