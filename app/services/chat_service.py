from app.agents.rag_agent import agent


class ChatService:

    def ask(
        self,
        question: str
    ):

        response = agent(question)

        return str(response)


chat_service = ChatService()