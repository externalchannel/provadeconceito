// negociador.asl — v2.0
// Atualizado para receber o relatório de eleição da frota MASPY

/* Quando o consultor informa o estado, inicia o leilão */
+estado_entrega(CEP, Estado)
  <- .print("Negociador recebeu CEP ", CEP, " para o estado ", Estado);
     .abolish(proposta_recebida(_,_,_));
     .abolish(vencedora(_,_,_));
     +entrega_atual(CEP, Estado);
     .print("=> Enviando Call for Proposal (CFP) para a rede SPADE...");
     spade.enviar_msg("cfp", CEP, Estado).

/* Recebe propostas das transportadoras via SPADE */
+mensagem_externa("propose", proposta(Nome, Custo, Prazo))[source(Remetente)]
  <- .print("Recebi proposta via REST: Transp ", Nome, " | R$ ", Custo, " | Prazo: ", Prazo);
     +proposta(Nome, Custo, Prazo);
     !verificar_vencedora.

/* Lógica de decisão — elege a transportadora de menor custo */
+!verificar_vencedora
  : not vencedora(_,_,_) &
    proposta("A", CA, PA) &
    proposta("B", CB, PB) &
    proposta("C", CC, PC) &
    CA <= CB & CA <= CC
  <- +vencedora("A", CA, PA);
     .print("Vencedora escolhida: A (Custo ", CA, ")");
     spade.enviar_msg("accept_proposal", "A").

+!verificar_vencedora
  : not vencedora(_,_,_) &
    proposta("A", CA, PA) &
    proposta("B", CB, PB) &
    proposta("C", CC, PC) &
    CB <= CA & CB <= CC
  <- +vencedora("B", CB, PB);
     .print("Vencedora escolhida: B (Custo ", CB, ")");
     spade.enviar_msg("accept_proposal", "B").

+!verificar_vencedora
  : not vencedora(_,_,_) &
    proposta("A", CA, PA) &
    proposta("B", CB, PB) &
    proposta("C", CC, PC) &
    CC <= CA & CC <= CB
  <- +vencedora("C", CC, PC);
     .print("Vencedora escolhida: C (Custo ", CC, ")");
     spade.enviar_msg("accept_proposal", "C").

+!verificar_vencedora <- true.

/* Recebe o relatório completo da eleição da frota MASPY
   Formato: entrega_concluida(Veiculo, Distancia, Tempo)
   Exemplo: entrega_concluida("veiculo_a_1", 0.28, 0.6)          */
+mensagem_externa("inform", entrega_concluida(Veiculo, Distancia, Tempo))[source(Remetente)]
  <- .print("==================================================");
     .print(" SUCESSO! O SPADE confirmou a entrega!");
     .print("   Veiculo vencedor : ", Veiculo);
     .print("   Distancia        : ", Distancia, " km");
     .print("   Tempo de entrega : ", Tempo, " s");
     .print("==================================================").

/* Fallback — formato antigo de confirmação (compatibilidade) */
+mensagem_externa("inform", entrega_concluida(Nome))[source(Remetente)]
  <- .print("==================================================");
     .print(" SUCESSO! O SPADE confirmou a entrega pela Transp ", Nome);
     .print("==================================================").

/* Rastreamento em tempo real — vem DIRETO do veículo MASPY (Opção D)
   Não passa pelo SPADE. O veículo envia eventos de status via jacamo-rest.
   Formato: veiculo_status(Veiculo, Status, CEP, Distancia)
   Status possíveis: "saiu_para_entrega", "em_rota", "entrega_realizada"   */
+mensagem_externa("inform", veiculo_status(Veiculo, Status, CEP, Distancia))[source(Remetente)]
  <- .print("[Rastreamento] ", Veiculo, " -> ", Status,
            " | CEP ", CEP, " | ", Distancia, " km").

