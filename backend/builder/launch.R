# R --vanilla suppresses user startup files; the project library is explicit.
.libPaths(c("/opt/shinyd/library", .Library), include.site = FALSE)
shiny::runApp("/app", host = "0.0.0.0", port = 3838, launch.browser = FALSE)
