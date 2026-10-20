library(shiny)

ui <- fluidPage(
  titlePanel("shinyd: fixture de sessão"),
  tags$p("Dados sintéticos para teste; este aplicativo não está publicado."),
  actionButton("increment", "Somar"),
  verbatimTextOutput("state"),
  verbatimTextOutput("credential_boundary"),
  fileInput("upload", "Arquivo de teste"),
  verbatimTextOutput("uploaded"),
  downloadButton("download", "Baixar estado")
)

server <- function(input, output, session) {
  count <- reactiveVal(0L)
  observeEvent(input$increment, count(count() + 1L))
  state <- reactive(paste(
    Sys.getenv("PROBE_INSTANCE"), getRversion(), packageVersion("shiny"),
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
