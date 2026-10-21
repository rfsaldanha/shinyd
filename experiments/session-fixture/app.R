library(shiny)

ui <- fluidPage(
  titlePanel("shinyd: fixture de sessão"),
  tags$p("Dados sintéticos para teste; este aplicativo não está publicado."),
  actionButton("increment", "Somar"),
  verbatimTextOutput("state"),
  verbatimTextOutput("credential_boundary"),
  verbatimTextOutput("environment_fingerprint"),
  textInput("data_value", "Dado persistente", ""),
  actionButton("save_data", "Gravar dado"),
  verbatimTextOutput("data_state"),
  verbatimTextOutput("data_revision"),
  fileInput("upload", "Arquivo de teste"),
  verbatimTextOutput("uploaded"),
  downloadButton("download", "Baixar estado")
)

server <- function(input, output, session) {
  read_data <- function() {
    directory <- Sys.getenv("SHINYD_DATA_DIR")
    if (!nzchar(directory)) return("data-disabled")
    path <- file.path(directory, "fixture.txt")
    if (file.exists(path)) paste(readLines(path, warn=FALSE), collapse="\n") else "data-empty"
  }
  saved_data <- reactiveVal(read_data())
  observeEvent(input$save_data, {
    directory <- Sys.getenv("SHINYD_DATA_DIR")
    req(nzchar(directory))
    writeLines(input$data_value, file.path(directory, "fixture.txt"))
    writeLines("temporary", "/tmp/fixture-persistence-marker")
    saved_data(read_data())
  })
  output$data_state <- renderText(saved_data())
  output$data_revision <- renderText(Sys.getenv("DATA_REVISION", unset="revision-unset"))
  output$environment_fingerprint <- renderText({
    # Only synthetic high-entropy values in this fixture. Never echo secrets.
    if (identical(Sys.getenv("APP_TOKEN", unset=NA_character_), NA_character_)) {
      "environment-unset"
    } else {
      paste(digest::digest(Sys.getenv("APP_TOKEN"), algo="sha256", serialize=FALSE),
        digest::digest(Sys.getenv("APP_MESSAGE"), algo="sha256", serialize=FALSE), sep="|")
    }
  })
  count <- reactiveVal(0L)
  observeEvent(input$increment, count(count() + 1L))
  state <- reactive(paste(
    Sys.getenv("SHINYD_INSTANCE_ID", unset = Sys.getenv("PROBE_INSTANCE")),
    getRversion(), packageVersion("shiny"),
    Sys.getpid(), count(), sep = "|"
  ))
  output$state <- renderText(state())
  output$credential_boundary <- renderText({
    cookies <- session$request$HTTP_COOKIE
    auth <- session$request$HTTP_AUTHORIZATION
    identity <- session$request$HTTP_X_SHINYD_USER
    leaked <- (!is.null(cookies) && grepl("shinyd", cookies, fixed = TRUE)) ||
      !is.null(auth) || !is.null(identity)
    if (leaked) "credential-leaked" else "credential-boundary-ok"
  })
  output$uploaded <- renderText({
    req(input$upload)
    readLines(input$upload$datapath, warn = FALSE)
  })
  output$download <- downloadHandler(
    filename = function() "state.txt",
    content = function(file) writeLines(state(), file)
  )
}

shinyApp(ui, server)
