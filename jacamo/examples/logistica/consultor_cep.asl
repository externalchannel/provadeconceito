// consultor_cep.asl
!start.

+!start
  <- .print("Agente consultor iniciado.");
     // CEP de teste para disparar o fluxo
     !consultar_cep("01001000").

+!consultar_cep(CEP)
  <- .print("Consultando API para o CEP: ", CEP);
     lookupArtifact("cepApi", A);
     focus(A);
     consultarCep(CEP).

+cep_resultado(CEP, Estado)
  <- .print("Sucesso! Estado: ", Estado);
     // Envia para o negociador (que agora vai disparar o leilao externo)
     .send(negociador, tell, estado_entrega(CEP, Estado)).

+cep_erro(Motivo)
  <- .print("Erro na API: ", Motivo).
