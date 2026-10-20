# Trusted helper; never source app.R, .Rprofile or the uploaded renv/activate.R.
options(timeout = 5, repos = c(CRAN = "https://cloud.r-project.org"),
        renv.lockfile.version = 1L)
lock <- jsonlite::read_json("/opt/shinyd/lock.json", simplifyVector = FALSE)
stopifnot(identical(as.character(getRversion()), lock$R$Version))
library <- "/opt/shinyd/library"
packages <- names(lock$Packages)
installed <- installed.packages()
for (package in packages) {
  stopifnot(package %in% rownames(installed))
  stopifnot(identical(unname(installed[package, "Version"]), lock$Packages[[package]]$Version))
}
# Copy approved installed packages into a private project library. Cache linking
# is disabled: the app must never refer to a writable, shared library on the host.
project <- "/tmp/shinyd-project"
dir.create(project, showWarnings = FALSE)
# hydrate intentionally skips renv itself. Copy this approved installed package
# explicitly when the lock includes it, avoiding any bootstrap download.
if ("renv" %in% packages) {
  stopifnot(file.copy(find.package("renv"), library, recursive = TRUE))
}
renv::hydrate(project = project, packages = packages, library = library,
              sources = .libPaths(), update = TRUE, prompt = FALSE)
renv::restore(project = project, library = library,
              lockfile = "/opt/shinyd/lock.json", clean = TRUE, prompt = FALSE)
observed <- renv::snapshot(project = project, library = library,
                          lockfile = NULL, type = "all", prompt = FALSE)
for (package in packages) {
  record <- observed$Packages[[package]]
  stopifnot(identical(record$Version, lock$Packages[[package]]$Version))
  expected <- lock$Packages[[package]]$Hash
  if (!is.null(expected)) stopifnot(identical(record$Hash, expected))
}
# Check that the lock contains the full dependency closure, without falling back
# to the site library. Base/recommended packages remain tied to the base image.
.libPaths(c(library, .Library), include.site = FALSE)
for (package in packages) loadNamespace(package)
stopifnot(requireNamespace("shiny", quietly = TRUE))
