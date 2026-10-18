from app.agents.rag_agent import create_agent


class ChatService:

    def ask(
        self,
        question: str
    ):

        # TODO: reuse an agent per session once conversation history is stored
        response = create_agent()(question)

        return str(response)


chat_service = ChatService()
