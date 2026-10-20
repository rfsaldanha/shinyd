package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"os"

	"github.com/rfsaldanha/shinyd/internal/doctor"
)

func run(args []string, out, errOut io.Writer) int {
	if len(args) == 0 || args[0] != "doctor" {
		fmt.Fprintln(errOut, "Uso: shinyd doctor [--json] [--data-dir CAMINHO_EXISTENTE]")
		return 3
	}
	flags := flag.NewFlagSet("doctor", flag.ContinueOnError)
	flags.SetOutput(errOut)
	jsonOutput := flags.Bool("json", false, "Saída JSON versionada")
	dataPath := flags.String("data-dir", ".", "Diretório existente para verificar espaço")
	if err := flags.Parse(args[1:]); err != nil || flags.NArg() != 0 {
		return 3
	}
	report := doctor.Inspect(doctor.Local(), *dataPath)
	if *jsonOutput {
		encoder := json.NewEncoder(out)
		encoder.SetIndent("", "  ")
		if err := encoder.Encode(report); err != nil {
			fmt.Fprintln(errOut, err)
			return 2
		}
	} else {
		fmt.Fprintln(out, "shinyd doctor — diagnóstico somente leitura")
		for _, c := range report.Checks {
			fmt.Fprintf(out, "[%s] %s: %s\n", c.Status, c.Code, c.Detail)
		}
		fmt.Fprintln(out, report.Notice)
	}
	return report.ExitCode()
}

func main() { os.Exit(run(os.Args[1:], os.Stdout, os.Stderr)) }
