CREATE TABLE IF NOT EXISTS datasets (
  dataset_id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  source_url TEXT NOT NULL,
  well_id TEXT,
  zenodo_record_id TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS files (
  remote_path TEXT PRIMARY KEY,
  dataset_id TEXT NOT NULL REFERENCES datasets(dataset_id),
  filename TEXT NOT NULL,
  source_size_display TEXT,
  source_modified_display TEXT,
  size_bytes INTEGER,
  sha256 TEXT,
  storage_url TEXT,
  transfer_status TEXT NOT NULL DEFAULT 'catalogued',
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS files_dataset ON files(dataset_id);
CREATE TABLE IF NOT EXISTS project_results (
  project_id TEXT NOT NULL,
  result_key TEXT NOT NULL,
  dataset_id TEXT,
  result_json TEXT NOT NULL,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (project_id, result_key)
);
INSERT OR IGNORE INTO datasets(dataset_id,title,source_url,well_id)
VALUES ('bcer-wa36588-pilot','BCER WA 36588 three-file cloud pilot',
        'https://files.bc-er.ca/','36588');
INSERT OR IGNORE INTO files(remote_path,dataset_id,filename,source_size_display,source_modified_display)
VALUES ('/WellData/36000-36999/36500-36599/36588/36588_COMP_2020APR05_REVISED01.PDF','bcer-wa36588-pilot','36588_COMP_2020APR05_REVISED01.PDF','18.7 MB','2020-12-24 18:07:43');
INSERT OR IGNORE INTO files(remote_path,dataset_id,filename,source_size_display,source_modified_display)
VALUES ('/WellData/36000-36999/36500-36599/36588/36588_WL_2019JUN01_MWDGR MD.LAS','bcer-wa36588-pilot','36588_WL_2019JUN01_MWDGR MD.LAS','1.1 MB','2020-12-04 18:03:41');
INSERT OR IGNORE INTO files(remote_path,dataset_id,filename,source_size_display,source_modified_display)
VALUES ('/WellData/36000-36999/36500-36599/36588/36588_WL_2019JUN01_MWDGR TVD.LAS','bcer-wa36588-pilot','36588_WL_2019JUN01_MWDGR TVD.LAS','332.7 KB','2020-12-04 18:03:42');
