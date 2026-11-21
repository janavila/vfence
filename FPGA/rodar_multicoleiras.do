# Abrir ModelSim-Altera e File > Change Directory para a pasta deste projeto.
if {[file exists work]} {vdel -lib work -all}
vlib work
vmap work work
vlog vfence_controle.v vfence_operativa.v vfence_top.v tb_vfence_multicoleiras.v
vsim -t 1ps work.tb_vfence_multicoleiras
run -all
