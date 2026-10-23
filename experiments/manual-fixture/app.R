library(shiny)

REVISAO <- "v1"

ui <- fluidPage(
  titlePanel(paste("Meu primeiro teste no shinyd —", REVISAO)),
  tags$p("Este aplicativo usa apenas dados que você informar para o teste."),
  textOutput("ambiente"),
  textOutput("mensagem"),
  textOutput("segredo"),
  actionButton("somar", "Somar 1"),
  textOutput("contador"),
  tags$hr(),
  textInput("texto", "Texto para guardar", "Olá, shinyd!"),
  actionButton("guardar", "Guardar texto"),
  textOutput("dado"),
  tags$hr(),
  fileInput("arquivo", "Enviar um arquivo de texto"),
  verbatimTextOutput("conteudo"),
  downloadButton("baixar", "Baixar texto guardado")
)

server <- function(input, output, session) {
  caminho <- function() {
    diretorio <- Sys.getenv("SHINYD_DATA_DIR")
    if (!nzchar(diretorio)) return(NULL)
    file.path(diretorio, "manual.txt")
  }
  ler <- function() {
    arquivo <- caminho()
    if (is.null(arquivo)) return("Habilite dados persistentes na página do app para guardar o texto.")
    if (!file.exists(arquivo)) return("Ainda não há texto guardado.")
    paste(readLines(arquivo, n = 100, warn = FALSE), collapse = "\n")
  }
  valor <- reactiveVal(ler())
  total <- reactiveVal(0L)
  observeEvent(input$somar, total(total() + 1L))
  observeEvent(input$guardar, {
    arquivo <- caminho()
    if (is.null(arquivo)) {
      showNotification("Habilite dados persistentes e aplique a configuração primeiro.")
      return()
    }
    writeLines(input$texto, arquivo)
    valor(ler())
  })
  output$ambiente <- renderText(paste("R", getRversion(), "· Shiny", packageVersion("shiny")))
  output$mensagem <- renderText(Sys.getenv("APP_MESSAGE", unset = "Você pode definir APP_MESSAGE no painel."))
  output$segredo <- renderText(if (nzchar(Sys.getenv("APP_TOKEN"))) "APP_TOKEN está configurado." else "APP_TOKEN não está configurado.")
  output$contador <- renderText(paste("Cliques nesta sessão:", total()))
  output$dado <- renderText(valor())
  output$conteudo <- renderText({
    req(input$arquivo)
    paste(readLines(input$arquivo$datapath, n = 50, warn = FALSE), collapse = "\n")
  })
  output$baixar <- downloadHandler(
    filename = function() "texto-guardado.txt",
    content = function(arquivo) writeLines(valor(), arquivo)
  )
}

shinyApp(ui, server)
