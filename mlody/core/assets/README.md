# Asset Layer

`mlody.core.assets` is the entry point for file-like runtime values.

Use this layer when you need to answer:

- Where is the concrete local artifact for this value?
- Should a remote artifact be revalidated or redownloaded?
- Should a declared local cache be refreshed from its source?
- What metadata and lineage should follow that artifact?

Core entry points:

- `asset_from_value(value_struct)`
  Resolves a runtime value struct into an `AssetSource`.
- `asset_from_location(location)`
  Resolves a bare runtime location struct into an `AssetSource`.
- `AssetSource.materialize()`
  Produces a `MaterializedAsset` with:
  - `path`
  - `content_hash`
  - `metadata`

Main implementations:

- `HttpAssetSource`
  Persistent HTTP-backed artifact cache with freshness-aware revalidation.
- `LocalPathAssetSource`
  Existing single local file.
- `CopiedAssetSource`
  Declared local copy backed by another asset source.

Responsibilities owned here:

- persistent remote cache layout
- remote metadata lookup and freshness decisions
- source-backed local copy refresh rules
- transfer lineage like `downloaded from` and `copied from`
- metadata-only rendering inputs for non-tabular remote values

What should stay out of this layer:

- CSV/parquet query logic
- DuckDB/SQL behavior
- row previews
- tabular shape interpretation

Those remain in `mlody.core.tabular`, which now acts as an adapter layer on top of assets.

Compatibility note:

`mlody.core.tabular.remote_staging` still exists for older callers and tests, but new code should use `HttpAssetSource` or `asset_from_value(...)` directly.
